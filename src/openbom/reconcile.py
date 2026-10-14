"""Reconciliation: match two workspaces' BOMs of the same product (e.g., a customer's
EBOM and an ODM's MBOM), classify their differences, and commit the agreed
correspondence as a versioned record.

A reconciliation workspace is owned by the org running it. Its root configuration
pins both sides as external entries (so `outdated`/`update` work unchanged), and
each commit points at a `correspondence` object holding the agreed part pairs.

Matching (all of it explainable as evidence):
  1. cross-reference keys: the other side's part number listed as a customer or
     supplier part number, identical part numbers, GTINs, and user decisions
  2. manufacturer part numbers shared between the two sides' approved lists
     (strong when the MPN identifies one part on each side, weak otherwise)
  Substitutes on the right that point at the same left part are folded into
  their primary's pair. A pair is auto-accepted only when exactly one strong
  candidate exists in both directions; everything else becomes a proposal.

Classification compares quantities rolled up under the nearest paired ancestor,
so a different grouping (regrouped) is not mistaken for a different count.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .errors import ConflictError, NotFoundError, OpenBomError, SchemaError
from .graph import ObjectSource, load, require, walk
from .identity import Actor, require_owner
from .objects import Configuration, Correspondence, Node, encode, make_correspondence, make_node
from .parts import parse_part_ref, save_node
from .store import DraftEdge, Store, ext_child_key
from .workspace import (
    CommitResult,
    commit_draft,
    create_workspace,
    get_workspace,
    head,
    load_commit,
    owned_workspace,
    pinned_source,
    resolve_commit,
)

LEFT, RIGHT = "left", "right"
# Evidence kinds. EXPLICIT evidence is a cross-reference one of the parties stated
# (or a user decision): trusted even when one part maps to several. "mpn" is
# inferred (a manufacturer part number unique on both sides): it pairs only when
# unambiguous. "mpn_shared" alone only ever produces a proposal.
EXPLICIT = {"decision", "customer_pn", "supplier_pn", "part_number", "gtin"}
STRONG = EXPLICIT | {"mpn"}

# Finding kinds
MATCH, REGROUPED, MOVED, QTY_MISMATCH = "match", "regrouped", "moved", "quantity_mismatch"
LEFT_ONLY, RIGHT_ONLY, IDENTITY, SOURCING = "left_only", "right_only", "identity_conflict", "sourcing"
SEVERITY = {
    MATCH: "ok",
    REGROUPED: "info",
    RIGHT_ONLY: "info",
    MOVED: "warning",
    LEFT_ONLY: "warning",
    QTY_MISMATCH: "warning",
    IDENTITY: "warning",
    SOURCING: "warning",
}
ROOT = "<root>"


def _norm(value: str) -> str:
    return "".join(value.split()).upper()


# --------------------------------------------------------------------------
# One side's graph, indexed for matching
# --------------------------------------------------------------------------


@dataclass
class SideNode:
    key: str
    hash: str
    node: Node
    pns: set[str]
    mpns: set[str]
    customer: set[str]
    supplier: set[str]
    gtins: set[str]


@dataclass
class Side:
    name: str
    source: str
    configuration: str
    root: str  # root part key
    nodes: dict[str, SideNode] = field(default_factory=dict)
    by_hash: dict[str, str] = field(default_factory=dict)
    children: dict[str, list[tuple[str, Decimal, tuple[str, ...]]]] = field(default_factory=dict)
    substitute_of: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    in_structure: set[str] = field(default_factory=set)  # keys that appear as a line (not only as substitutes)
    missing: list[str] = field(default_factory=list)


def build_side(get: ObjectSource, name: str, source: str, configuration: str, strip_prefixes: Iterable[str] = ()) -> Side:
    prefixes = [_norm(p) for p in strip_prefixes if p]

    def add(h: str) -> str:
        if h in side.by_hash:
            return side.by_hash[h]
        node = load(get, h, Node)
        if node is None:
            side.missing.append(h)
            side.by_hash[h] = f"<missing {h[:19]}>"
            return side.by_hash[h]
        pn = _norm(node.part_id)
        pns = {pn} | {pn[len(p):] for p in prefixes if pn.startswith(p) and len(pn) > len(p)}
        ids = defaultdict(set)
        for scheme, value in node.identifiers:
            ids[scheme].add(_norm(value))
        mpns = {_norm(m) for _, m in node.aml} | ({_norm(node.mpn)} if node.mpn else set()) | ids["mpn"]
        side.nodes[node.key] = SideNode(node.key, h, node, pns, mpns, ids["customer"], ids["supplier"], ids["gtin"])
        side.by_hash[h] = node.key
        return node.key

    root_comp = require(get, configuration, Configuration)
    side = Side(name=name, source=source, configuration=configuration, root="")
    side.root = add(root_comp.node)
    side.in_structure.add(side.root)
    w = walk(get, configuration, reveal=False)
    side.missing.extend(w.missing)
    for comp in w.configurations.values():
        parent = add(comp.node)
        kids = []
        for e in comp.entries:
            child = add(e.node)
            side.in_structure.add(child)
            subs = tuple(add(n) for n, _ in e.substitutes)
            for sub in subs:
                side.substitute_of[sub].add(child)
            kids.append((child, Decimal(e.quantity), subs))
        side.children[parent] = kids
    return side


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    left: str
    candidates: list[tuple[str, list[str]]]  # (right key, evidence)
    reason: str  # "ambiguous" | "weak"


@dataclass
class Decisions:
    pairs: set[tuple[str, str]] = field(default_factory=set)
    rejects: set[tuple[str, str]] = field(default_factory=set)
    accepted: dict[str, str] = field(default_factory=dict)  # part key -> note


def load_decisions(store: Store, ws_id: str) -> Decisions:
    d = Decisions()
    for row in store.list_decisions(ws_id):
        if row["kind"] == "pair":
            d.pairs.add((row["left_part"], row["right_part"]))
        elif row["kind"] == "reject":
            d.rejects.add((row["left_part"], row["right_part"]))
        else:
            d.accepted[row["left_part"] or row["right_part"]] = row["note"]
    return d


def _index(side: Side, attr: str) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = defaultdict(set)
    for n in side.nodes.values():
        for v in getattr(n, attr):
            idx[v].add(n.key)
    return idx


def candidates(left: Side, right: Side, decisions: Decisions) -> dict[str, dict[str, set[str]]]:
    """left key -> right key -> evidence kinds."""
    cands: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    r_pns, r_mpns, r_customer = _index(right, "pns"), _index(right, "mpns"), _index(right, "customer")
    r_supplier, r_gtin = _index(right, "supplier"), _index(right, "gtins")
    l_mpns = _index(left, "mpns")
    for L in left.nodes.values():
        for v in L.pns:
            for r in r_customer.get(v, ()):
                cands[L.key][r].add("customer_pn")
            for r in r_pns.get(v, ()):
                cands[L.key][r].add("part_number")
        for v in L.customer:
            for r in r_pns.get(v, ()):
                cands[L.key][r].add("customer_pn")
        for v in L.supplier:
            for r in r_pns.get(v, set()) | r_mpns.get(v, set()):
                cands[L.key][r].add("supplier_pn")
        for v in L.pns | L.mpns:
            for r in r_supplier.get(v, ()):
                cands[L.key][r].add("supplier_pn")
        for v in L.gtins:
            for r in r_gtin.get(v, ()):
                cands[L.key][r].add("gtin")
        for v in L.mpns:
            rs = r_mpns.get(v, set())
            unique = len(rs) == 1 and len(l_mpns.get(v, ())) == 1
            for r in rs:
                cands[L.key][r].add("mpn" if unique else "mpn_shared")
    for left_key, right_key in decisions.rejects:
        cands.get(left_key, {}).pop(right_key, None)
    for left_key, right_key in decisions.pairs:
        if left_key in left.nodes and right_key in right.nodes:
            cands[left_key][right_key].add("decision")
    return cands


@dataclass
class MatchResult:
    pairs: dict[tuple[str, str], list[str]]  # (left, right) -> evidence
    folded: dict[tuple[str, str], list[str]]  # pair -> right substitutes folded into it
    proposals: list[Proposal]
    candidates: dict[str, dict[str, set[str]]] = field(default_factory=dict)  # all evidence, incl. weak


def match(left: Side, right: Side, decisions: Decisions) -> MatchResult:
    cands = candidates(left, right, decisions)
    evidence = {L: {R: set(ev) for R, ev in rs.items()} for L, rs in cands.items()}
    pairs: dict[tuple[str, str], list[str]] = {}
    folded: dict[tuple[str, str], list[str]] = defaultdict(list)
    proposals: list[Proposal] = []

    # Fold right-side substitutes into their primary's candidacy.
    subs_of: dict[str, dict[str, str]] = {}
    for L, rs in cands.items():
        subs_of[L] = {}
        for R in list(rs):
            primaries = right.substitute_of.get(R, set()) & set(rs)
            if primaries and R not in right.in_structure:
                subs_of[L][R] = sorted(primaries)[0]
                del rs[R]

    # Pass 1: explicit cross-references (one-to-many allowed; groups handle the quantities).
    explicitly_paired: set[str] = set()
    for L in sorted(cands):
        for R, ev in cands[L].items():
            if ev & EXPLICIT:
                pairs[(L, R)] = sorted(ev)
                explicitly_paired.add(R)
    # Pass 2: inferred evidence, only where unambiguous in both directions.
    inferred_claims: dict[str, set[str]] = defaultdict(set)
    for L, rs in cands.items():
        for R, ev in rs.items():
            if "mpn" in ev:
                inferred_claims[R].add(L)
    for L in sorted(cands):
        rs = cands[L]
        if not rs or any(lk == L for lk, _ in pairs):
            continue
        inferred = [R for R, ev in rs.items() if "mpn" in ev]
        if len(inferred) == 1 and inferred_claims[inferred[0]] == {L} and inferred[0] not in explicitly_paired:
            pairs[(L, inferred[0])] = sorted(rs[inferred[0]])
            continue
        reason = "ambiguous" if inferred else "weak"
        proposals.append(Proposal(L, sorted((R, sorted(ev)) for R, ev in rs.items()), reason))

    for L, mapping in subs_of.items():
        for sub, primary in mapping.items():
            if (L, primary) in pairs:
                folded[(L, primary)].append(sub)
    # A right part claimed by several left parts that have no other option is reported once per left part above.
    return MatchResult(pairs, {k: sorted(v) for k, v in folded.items()}, proposals, evidence)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    severity: str
    left: list[str]
    right: list[str]
    anchor: str  # the paired ancestor the quantities are rolled up under (left key, or <root>)
    left_qty: str | None = None
    right_qty: str | None = None
    detail: str = ""
    accepted: str | None = None

    def signature(self) -> tuple:
        return (self.kind, tuple(self.left), tuple(self.right), self.anchor, self.left_qty, self.right_qty, self.detail)


class _Groups:
    """Connected components of the pair graph (handles one-to-many pairings)."""

    def __init__(self, pairs: Iterable[tuple[str, str]]):
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}
        for left_key, right_key in pairs:
            self.union((LEFT, left_key), (RIGHT, right_key))

    def find(self, x: tuple[str, str]) -> tuple[str, str]:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        self.parent[self.find(a)] = self.find(b)

    def members(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        out: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return out


def _rollup(side: Side, anchors: dict[str, Any]) -> dict[tuple[str, Any], list]:
    """(node key, anchor id) -> [quantity under that anchor, all-direct?] using nearest paired ancestors."""
    acc: dict[tuple[str, Any], list] = {}
    expanded: set[str] = set()

    def dfs(node: str, anchor: Any, mult: Decimal, at_anchor: bool) -> None:
        for child, qty, _ in side.children.get(node, ()):
            m = mult * qty
            slot = acc.setdefault((child, anchor), [Decimal(0), True])
            slot[0] += m
            slot[1] = slot[1] and at_anchor
            if child in anchors:
                if child not in expanded:
                    expanded.add(child)
                    dfs(child, anchors[child], Decimal(1), True)
            else:
                dfs(child, anchor, m, False)

    dfs(side.root, anchors.get(side.root, ROOT), Decimal(1), True)
    return acc


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


def classify(
    left: Side, right: Side, result: MatchResult, accepted: dict[str, str], approver: str | None = None
) -> list[Finding]:
    """approver: the side whose approved manufacturer lists are authoritative (LEFT/RIGHT), or None."""
    groups = _Groups(result.pairs)
    members = groups.members()
    group_of: dict[tuple[str, str], tuple[str, str]] = {m: g for g, ms in members.items() for m in ms}
    left_anchor = {k: group_of[(LEFT, k)] for k in left.nodes if (LEFT, k) in group_of}
    right_anchor = {k: group_of[(RIGHT, k)] for k in right.nodes if (RIGHT, k) in group_of}
    for side, anchors in ((left, left_anchor), (right, right_anchor)):
        anchors.setdefault(side.root, ROOT)
    lroll, rroll = _rollup(left, left_anchor), _rollup(right, right_anchor)

    def anchor_name(a: Any) -> str:
        if a == ROOT:
            return ROOT
        ls = sorted(k for s, k in members[a] if s == LEFT)
        return ls[0] if ls else sorted(k for _, k in members[a])[0]

    findings: list[Finding] = []
    for g, ms in members.items():
        ls = sorted(k for s, k in ms if s == LEFT)
        rs = sorted(k for s, k in ms if s == RIGHT)
        per_anchor: dict[Any, list] = defaultdict(lambda: [Decimal(0), Decimal(0), True, True])
        for key_set, roll, i in ((ls, lroll, 0), (rs, rroll, 1)):
            for (node, anchor), (qty, direct) in roll.items():
                if node in key_set:
                    per_anchor[anchor][i] += qty
                    per_anchor[anchor][2 + i] = per_anchor[anchor][2 + i] and direct
        total_l = sum(v[0] for v in per_anchor.values())
        total_r = sum(v[1] for v in per_anchor.values())
        uneven = [a for a, v in per_anchor.items() if v[0] != v[1]]
        if uneven and total_l == total_r:
            # Same total, different place: the two sides put it under different (paired) parents.
            where = lambda i: ", ".join(  # noqa: E731
                f"{_fmt(v[i])} under {anchor_name(a)}" for a, v in sorted(per_anchor.items(), key=lambda x: anchor_name(x[0])) if v[i]
            )
            findings.append(
                Finding(MOVED, SEVERITY[MOVED], ls, rs, anchor_name(uneven[0]), _fmt(total_l), _fmt(total_r),
                        detail=f"{left.source}: {where(0)}; {right.source}: {where(1)}")
            )
        else:
            for anchor, (lq, rq, ldirect, rdirect) in per_anchor.items():
                if lq == rq:
                    kind = MATCH if ldirect and rdirect else REGROUPED
                else:
                    kind = QTY_MISMATCH
                findings.append(Finding(kind, SEVERITY[kind], ls, rs, anchor_name(anchor), _fmt(lq), _fmt(rq)))
        # Sourcing: manufacturer parts the other side uses (incl. folded substitutes) that the
        # approving side hasn't approved.
        if approver is not None:
            appr, appr_keys = (left, ls) if approver == LEFT else (right, rs)
            user, user_keys = (right, set(rs)) if approver == LEFT else (left, set(ls))
            if approver == LEFT:
                user_keys |= {s for k in ls for r in rs for s in result.folded.get((k, r), ())}
            approved = set().union(*(appr.nodes[k].mpns | appr.nodes[k].supplier for k in appr_keys))
            if any(appr.nodes[k].node.aml or appr.nodes[k].node.mpn for k in appr_keys):
                unapproved = sorted(set().union(*(user.nodes[k].mpns for k in user_keys)) - approved)
                if unapproved:
                    findings.append(
                        Finding(SOURCING, SEVERITY[SOURCING], ls, rs, "",
                                detail=f"{user.source} uses {', '.join(unapproved)}, not on {appr.source}'s approved list")
                    )

    for side, anchors, roll, kind in ((left, left_anchor, lroll, LEFT_ONLY), (right, right_anchor, rroll, RIGHT_ONLY)):
        for key in sorted(side.in_structure - set(anchors)):
            for (node, anchor), (qty, _) in roll.items():
                if node == key:
                    f = Finding(kind, SEVERITY[kind], [key] if kind == LEFT_ONLY else [], [key] if kind == RIGHT_ONLY else [], anchor_name(anchor))
                    if kind == LEFT_ONLY:
                        f.left_qty = _fmt(qty)
                    else:
                        f.right_qty = _fmt(qty)
                    findings.append(f)

    # Possible identity conflict: under one anchor, one side has extra of X while the other lists an
    # unpaired Y of the same count, and some evidence (even weak) links X and Y.
    def linked(lk: str, rk: str) -> bool:
        return rk in result.candidates.get(lk, {})

    for f in [x for x in findings if x.kind == QTY_MISMATCH]:
        lq, rq = Decimal(f.left_qty), Decimal(f.right_qty)
        if lq < rq:
            for lo in [x for x in findings if x.kind == LEFT_ONLY and x.anchor == f.anchor
                       and Decimal(x.left_qty) == rq - lq and any(linked(x.left[0], r) for r in f.right)]:
                findings.append(Finding(
                    IDENTITY, SEVERITY[IDENTITY], lo.left, f.right, f.anchor, lo.left_qty, _fmt(rq - lq),
                    detail=f"{right.source} uses {', '.join(f.right)} where {left.source} lists {', '.join(lo.left)}",
                ))
        elif rq < lq:
            for ro in [x for x in findings if x.kind == RIGHT_ONLY and x.anchor == f.anchor
                       and Decimal(x.right_qty) == lq - rq and any(linked(lk, x.right[0]) for lk in f.left)]:
                findings.append(Finding(
                    IDENTITY, SEVERITY[IDENTITY], f.left, ro.right, f.anchor, _fmt(lq - rq), ro.right_qty,
                    detail=f"{left.source} uses {', '.join(f.left)} where {right.source} lists {', '.join(ro.right)}",
                ))

    for f in findings:
        notes = [accepted[k] for k in f.left + f.right if k in accepted]
        if notes and f.severity != "ok":
            f.accepted = notes[0]
    order = [MATCH, REGROUPED, MOVED, QTY_MISMATCH, IDENTITY, SOURCING, LEFT_ONLY, RIGHT_ONLY]
    findings.sort(key=lambda f: (order.index(f.kind), f.anchor, f.left, f.right))
    return findings


# --------------------------------------------------------------------------
# Reconciliation workspaces
# --------------------------------------------------------------------------


@dataclass
class ReconcilePlan:
    workspace: str
    left: dict[str, str]  # {"source", "configuration", "root"}
    right: dict[str, str]
    pairs: list[dict[str, Any]]
    proposals: list[Proposal]
    findings: list[Finding]
    new_findings: list[Finding] = field(default_factory=list)  # vs HEAD
    resolved_findings: list[Finding] = field(default_factory=list)
    head: str | None = None
    correspondence: str | None = None  # hash the draft would commit
    names: dict[str, str] = field(default_factory=dict)  # part key -> name, for display

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for f in self.findings:
            c[f.kind + (" (accepted)" if f.accepted else "")] += 1
        return dict(c)

    @property
    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning" and not f.accepted]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["counts"] = self.counts()
        return data


def init_reconciliation(
    store: Store,
    actor: Actor,
    ws_id: str,
    left_ws: str,
    right_ws: str,
    *,
    name: str | None = None,
    strip_prefix: dict[str, list[str]] | None = None,
    approver: str | None = None,
) -> str | None:
    """Create a reconciliation workspace owned by the acting org, pinning both sides at HEAD.

    The two sides are symmetric. strip_prefix maps a side's workspace ID to part-number
    prefixes to ignore. approver is the workspace whose approved manufacturer lists are
    authoritative; by default, the side the acting org owns (if exactly one). Returns it.
    """
    if left_ws == right_ws:
        raise ConflictError("the two sides of a reconciliation must be different workspaces")
    with store.transaction():
        owners = {ws: get_workspace(store, ws).owner_org_id for ws in (left_ws, right_ws)}
        if approver is None:
            mine = [ws for ws, org in owners.items() if org == actor.org]
            approver = mine[0] if len(mine) == 1 else None
        elif approver not in owners:
            raise OpenBomError(f"approver must be one of the two sides ({left_ws}, {right_ws})")
        for ws in strip_prefix or {}:
            if ws not in owners:
                raise OpenBomError(f"--strip-prefix names {ws!r}, which is not one of the two sides")
        if store.get_part_node(actor.org, ws_id) is not None:
            raise ConflictError(f"part {actor.org}:{ws_id} already exists; choose another reconciliation name")
        root = make_node(
            org=actor.org,
            part_id=ws_id,
            revision="1",
            name=name or f"Reconciliation: {left_ws} <-> {right_ws}",
            attributes={"reconciliation": "true"},
        )
        save_node(store, actor, root)
        create_workspace(store, actor, ws_id, ws_id, name)
        for ws in (left_ws, right_ws):
            _, comp_hash, comp = pinned_source(store, ws)
            store.put_draft_edge(
                DraftEdge(
                    workspace_id=ws_id,
                    parent_part_id=ws_id,
                    child_key=ext_child_key(ws),
                    ext_source_ws=ws,
                    ext_node=comp.node,
                    ext_configuration=comp_hash,
                    quantity="1",
                    uom="ea",
                )
            )
        side_of = {left_ws: LEFT, right_ws: RIGHT}
        options = {
            "strip_prefix": {side_of[ws]: p for ws, p in (strip_prefix or {}).items()},
            "approver": side_of.get(approver),
        }
        store.add_reconciliation(ws_id, left_ws, right_ws, options)
    return approver


def _recon(store: Store, ws_id: str):
    rec = store.get_reconciliation(ws_id)
    if rec is None:
        raise NotFoundError(f"workspace '{ws_id}' is not a reconciliation; create one with `reconcile init`")
    return rec


def _draft_pins(store: Store, rec) -> tuple[str, str]:
    pins = {e.ext_source_ws: e.ext_configuration for e in store.list_draft_edges(rec.workspace_id) if e.is_external}
    try:
        return pins[rec.left_ws], pins[rec.right_ws]
    except KeyError as exc:
        raise OpenBomError(f"reconciliation draft is missing its pin for {exc.args[0]}") from None


def _sides(store: Store, rec, left_comp: str, right_comp: str) -> tuple[Side, Side]:
    strip = rec.options.get("strip_prefix", {})
    get = store.get_object
    return (
        build_side(get, LEFT, rec.left_ws, left_comp, strip.get(LEFT, ())),
        build_side(get, RIGHT, rec.right_ws, right_comp, strip.get(RIGHT, ())),
    )


def _correspondence(rec, left: Side, right: Side, result: MatchResult, decisions: Decisions) -> Correspondence:
    accepted = []
    for key, note in decisions.accepted.items():
        for side in (left, right):
            if key in side.nodes:
                accepted.append((side.nodes[key].hash, note))
                break
    return make_correspondence(
        left_source=rec.left_ws,
        left_configuration=left.configuration,
        right_source=rec.right_ws,
        right_configuration=right.configuration,
        pairs=[(left.nodes[lk].hash, right.nodes[rk].hash, ev) for (lk, rk), ev in result.pairs.items()],
        accepted=dict(accepted).items(),
    )


def findings_at(store: Store, commit_hash: str) -> tuple[Side, Side, Correspondence, list[Finding]]:
    """Recompute the findings of a committed reconciliation from its correspondence."""
    commit = load_commit(store, commit_hash)
    if commit.correspondence is None:
        raise SchemaError(f"commit {commit_hash} is not a reconciliation commit")
    corr = require(store.get_object, commit.correspondence, Correspondence)
    rec = _recon(store, commit.workspace)
    left, right = _sides(store, rec, corr.left_configuration, corr.right_configuration)
    pairs = {(left.by_hash[p.left], right.by_hash[p.right]): list(p.evidence) for p in corr.pairs}
    accepted = {}
    for node, note in corr.accepted:
        key = left.by_hash.get(node) or right.by_hash.get(node)
        if key:
            accepted[key] = note
    rematch = match(left, right, Decisions(pairs=set(pairs)))
    result = MatchResult(pairs, {k: v for k, v in rematch.folded.items() if k in pairs}, [], rematch.candidates)
    return left, right, corr, classify(left, right, result, accepted, rec.options.get("approver"))


def plan_reconciliation(store: Store, ws_id: str) -> ReconcilePlan:
    rec = _recon(store, ws_id)
    left_comp, right_comp = _draft_pins(store, rec)
    left, right = _sides(store, rec, left_comp, right_comp)
    decisions = load_decisions(store, ws_id)
    result = match(left, right, decisions)
    findings = classify(left, right, result, decisions.accepted, rec.options.get("approver"))
    corr_hash = encode(_correspondence(rec, left, right, result, decisions))[0]
    plan = ReconcilePlan(
        workspace=ws_id,
        left={"source": rec.left_ws, "configuration": left_comp, "root": left.root},
        right={"source": rec.right_ws, "configuration": right_comp, "root": right.root},
        pairs=[
            {"left": lk, "right": rk, "evidence": ev, "substitutes": result.folded.get((lk, rk), [])}
            for (lk, rk), ev in sorted(result.pairs.items())
        ],
        proposals=result.proposals,
        findings=findings,
        correspondence=corr_hash,
        names={k: n.node.name for side in (left, right) for k, n in side.nodes.items()},
    )
    current = head(store, ws_id)
    if current is not None:
        plan.head = current[0]
        if current[1].correspondence is not None:
            old = findings_at(store, current[0])[3]
            old_sigs = {f.signature() for f in old}
            new_sigs = {f.signature() for f in findings}
            plan.new_findings = [f for f in findings if f.signature() not in old_sigs and f.severity != "ok"]
            plan.resolved_findings = [f for f in old if f.signature() not in new_sigs and f.severity != "ok"]
    return plan


def commit_reconciliation(store: Store, actor: Actor, ws_id: str, message: str) -> CommitResult:
    rec = _recon(store, ws_id)
    with store.transaction():
        owned_workspace(store, actor, ws_id)
        left_comp, right_comp = _draft_pins(store, rec)
        left, right = _sides(store, rec, left_comp, right_comp)
        decisions = load_decisions(store, ws_id)
        result = match(left, right, decisions)
        corr = _correspondence(rec, left, right, result, decisions)
        return commit_draft(store, actor, ws_id, message, correspondence=encode(corr))


def _resolve_part(store: Store, rec, left: Side, right: Side, ref: str) -> str:
    if ":" in ref:
        org, part_id = parse_part_ref(ref, None)
        return f"{org}:{part_id}"
    for side, ws in ((left, rec.left_ws), (right, rec.right_ws)):
        key = f"{get_workspace(store, ws).owner_org_id}:{ref}"
        if key in side.nodes:
            return key
    raise NotFoundError(f"part {ref!r} is in neither {rec.left_ws} nor {rec.right_ws}")


def decide(store: Store, actor: Actor, ws_id: str, kind: str, parts: list[str], note: str = "") -> list[str]:
    """Record a pair / reject (two parts, one from each side, in any order) or an accept (one part).

    Decisions are stored by stable part key, so they carry over to later revisions.
    """
    rec = _recon(store, ws_id)
    require_owner(actor, get_workspace(store, ws_id).owner_org_id, f"reconciliation '{ws_id}'")
    left, right = _sides(store, rec, *_draft_pins(store, rec))
    keys = [_resolve_part(store, rec, left, right, p) for p in parts]
    where = lambda k: f"{k} is in neither {rec.left_ws} nor {rec.right_ws}"  # noqa: E731
    if kind in ("pair", "reject"):
        if len(keys) != 2:
            raise OpenBomError(f"{kind} takes two parts, one from each side")
        a, b = keys
        if a in left.nodes and b in right.nodes:
            left_key, right_key = a, b
        elif b in left.nodes and a in right.nodes:
            left_key, right_key = b, a
        else:
            missing = [k for k in keys if k not in left.nodes and k not in right.nodes]
            raise OpenBomError(where(missing[0]) if missing else f"{a} and {b} are on the same side")
    else:
        if len(keys) != 1:
            raise OpenBomError("accept takes one part")
        (k,) = keys
        if k not in left.nodes and k not in right.nodes:
            raise OpenBomError(where(k))
        left_key, right_key = (k, "") if k in left.nodes else ("", k)
    with store.transaction():
        if kind == "pair":
            store.delete_decision(ws_id, "reject", left_key, right_key)
        if kind == "reject":
            store.delete_decision(ws_id, "pair", left_key, right_key)
        store.put_decision(ws_id, kind, left_key, right_key, note)
    return [k for k in (left_key, right_key) if k]


def diff_reconciliations(store: Store, a: str, b: str) -> tuple[list[Finding], list[Finding]]:
    """Findings that appear in b but not a (new) and in a but not b (resolved)."""
    old, new = findings_at(store, a)[3], findings_at(store, b)[3]
    o, n = {f.signature() for f in old}, {f.signature() for f in new}
    return (
        [f for f in new if f.signature() not in o and f.severity != "ok"],
        [f for f in old if f.signature() not in n and f.severity != "ok"],
    )


def resolve_recon_commit(store: Store, ws_id: str, rev: str) -> str:
    h = resolve_commit(store, ws_id, rev)
    if load_commit(store, h).correspondence is None:
        raise SchemaError(f"{rev} is not a reconciliation commit")
    return h

