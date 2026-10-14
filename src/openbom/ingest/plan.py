"""Plan and apply a BOM import.

plan_import never changes the database: it reconciles the file against
existing parts and mappings, and previews the result by applying it inside a
transaction that is rolled back. apply_plan writes parts and replaces the
workspace draft. Neither commits.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from ..diff import DiffResult, changed_fields
from ..errors import OpenBomError, SchemaError
from ..identity import Actor
from ..objects import DEFAULT_UOM, PART_ID_RE, Node, encode, make_node, validate_workspace_id
from ..parts import load_node, part_key, save_node
from ..status import status
from ..store import DraftEdge, Store, ext_child_key, own_child_key
from ..workspace import create_workspace, owned_workspace, pinned_source, replace_draft
from .profiles import load_source
from .source import ERROR, INFO, WARNING, Issue
from .tabular import Row
# TODO(question): parts with no revision in the file get this label.
DEFAULT_REVISION = "-"
MAX_LISTED = 5

# Occurrence kinds
PART, EXTERNAL, IGNORED, INVALID = "part", "external", "ignored", "invalid"


@dataclass
class PartPlan:
    part_id: str
    action: str  # "new" | "unchanged" | "revise"
    line: int
    revision: str
    name: str
    node_hash: str
    old_revision: str | None = None
    old_node: str | None = None
    fields: list[str] = field(default_factory=list)
    node: Node | None = field(default=None, repr=False)


@dataclass
class EdgePlan:
    parent: str  # own part ID
    child: str  # own part ID, or source workspace ID when external
    quantity: str
    uom: str
    refdes: list[str]
    lines: list[int]
    external: bool = False
    pin_node: str | None = None
    pin_configuration: str | None = None
    pin_kept: bool = False  # re-used the draft's existing pin instead of the source's HEAD
    substitutes: list[tuple[str, int | None]] = field(default_factory=list)  # (own part ID, priority)


@dataclass
class ImportPlan:
    file: str
    profile: str
    sheet: str | None
    workspace: str
    create_workspace: bool
    root: str
    header_row: int
    columns: dict[str, str]
    ignored_columns: list[str]
    row_count: int
    parts: list[PartPlan] = field(default_factory=list)
    edges: list[EdgePlan] = field(default_factory=list)
    ignored_rows: list[int] = field(default_factory=list)
    removed_draft_edges: int = 0
    issues: list[Issue] = field(default_factory=list)
    preview: DiffResult | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for p in data["parts"]:
            p.pop("node", None)
        data["preview"] = self.preview.to_json() if self.preview else None
        data["ready"] = not self.errors
        return data


@dataclass
class _Occ:
    """One row of the file placed in the tree (or the synthetic root)."""

    row: Row | None
    key: str  # own part ID, or source workspace ID for external
    kind: str
    children: list["_Occ"] = field(default_factory=list)


def _suggest_part_id(pn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]+", "-", pn).strip("-")[:64] or "PART"


def _fmt_qty(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _count(occ: _Occ) -> int:
    return sum(1 + _count(c) for c in occ.children)


def plan_import(
    store: Store,
    actor: Actor,
    path: str | Path,
    *,
    ws_id: str | None = None,
    new_ws: str | None = None,
    sheet: str | None = None,
    columns: Mapping[str, str] | None = None,
    update_metadata: bool = False,
    preview: bool = True,
    profile: str | None = None,
) -> ImportPlan:
    if (ws_id is None) == (new_ws is None):
        raise OpenBomError("import into an existing workspace (-w) or a new one (--new-ws), not both")
    if new_ws is not None:
        validate_workspace_id(new_ws)
        if store.get_workspace(new_ws) is not None:
            raise OpenBomError(f"workspace '{new_ws}' already exists; import into it with -w {new_ws}")
    ws = owned_workspace(store, actor, ws_id) if ws_id is not None else None

    src = load_source(path, profile=profile, sheet=sheet, columns=columns)
    rows = src.rows
    if not rows:
        raise OpenBomError(f"no data rows below the header (row {src.header_row})")

    plan = ImportPlan(
        file=str(path),
        profile=src.profile,
        sheet=src.sheet,
        workspace=new_ws or ws.id,
        create_workspace=new_ws is not None,
        root=ws.root_part_id if ws else "",
        header_row=src.header_row,
        columns=src.columns,
        ignored_columns=src.ignored_columns,
        row_count=len(rows) + sum(len(r.substitutes) for r in rows),
        issues=list(src.issues),
    )

    def issue(severity: str, message: str, line: int | None = None, hint: str | None = None) -> None:
        plan.issues.append(Issue(severity, message, line, hint))

    if "revision" not in src.columns:
        issue(WARNING, "no revision column; new parts get revision '-' and existing parts keep theirs")
    if "name" not in src.columns and "description" not in src.columns:
        issue(WARNING, "no name or description column; part numbers are used as names")

    def resolve(r: Row) -> _Occ:
        """Resolve a row's part number through the org's mappings."""
        if not r.pn:
            issue(ERROR, "row has no part number", r.line, "fill it in, or delete the row")
            return _Occ(r, "", INVALID)
        mapping = store.get_mapping(actor.org, r.pn)
        if mapping and mapping.ignore:
            return _Occ(r, r.pn, IGNORED)
        if mapping and mapping.ext_source_ws:
            return _Occ(r, mapping.ext_source_ws, EXTERNAL)
        if mapping and mapping.part_id:
            return _Occ(r, mapping.part_id, PART)
        if PART_ID_RE.fullmatch(r.pn):
            return _Occ(r, r.pn, PART)
        issue(
            ERROR,
            f"part number {r.pn!r} has characters part IDs don't allow (A-Z a-z 0-9 . _ -)",
            r.line,
            f'openbom map add "{r.pn}" --part {_suggest_part_id(r.pn)}',
        )
        return _Occ(r, r.pn, INVALID)

    # 1. Resolve each row through the org's mappings.
    occs = []
    for r in rows:
        occs.append(resolve(r))
        if r.level is None:
            issue(ERROR, f"level {r.raw_level!r} is not understood (use 0, 1, 2… or .1, ..2…)", r.line)

    # 2. Rebuild the tree from levels.
    placed = [o for o in occs if o.row.level is not None]
    if not placed:
        raise OpenBomError("no rows have a usable level")
    min_level = min(o.row.level for o in placed)
    tops: list[_Occ] = []
    stack: list[_Occ] = []
    for occ in placed:
        level = occ.row.level
        while stack and stack[-1].row.level >= level:
            stack.pop()
        if not stack:
            if level != min_level:
                issue(ERROR, f"row at level {level} has no parent row above it", occ.row.line)
            tops.append(occ)
        else:
            parent = stack[-1]
            if level > parent.row.level + 1:
                issue(ERROR, f"level jumps from {parent.row.level} to {level}", occ.row.line)
            parent.children.append(occ)
        stack.append(occ)

    # 3. Find the root.
    root_occ: _Occ | None = None
    if new_ws is not None:
        if len(tops) == 1 and tops[0].kind == PART:
            root_occ = tops[0]
            plan.root = root_occ.key
        else:
            issue(ERROR, f"--new-ws needs exactly one top-level row (the product root); found {len(tops)}")
    elif len(tops) == 1 and tops[0].kind == PART and tops[0].key == ws.root_part_id:
        root_occ = tops[0]
    elif len(tops) == 1:
        top = tops[0]
        issue(
            ERROR,
            f"the file's top-level part {top.row.pn!r} is not workspace {ws.id}'s root "
            f"{part_key(ws.owner_org_id, ws.root_part_id)}",
            top.row.line,
            f'openbom map add "{top.row.pn}" --part {ws.root_part_id}   (or import with --new-ws)',
        )
    else:
        root_occ = _Occ(None, ws.root_part_id, PART, children=tops)
        issue(INFO, f"{len(tops)} top-level rows become children of root {part_key(actor.org, ws.root_part_id)}")

    # 4. Walk the tree: collect part facts and each parent's (aggregated) children.
    facts: dict[str, dict[str, dict[str, list[int]]]] = {}
    first_line: dict[str, int] = {}
    expansions: dict[str, tuple[int, dict[str, dict[str, Any]]]] = {}

    lists: dict[str, dict[str, set]] = {}  # part -> {"aml": set, "identifiers": set}, merged across rows

    def record(occ: _Occ) -> None:
        r = occ.row
        first_line.setdefault(occ.key, r.line)
        f = facts.setdefault(occ.key, {k: {} for k in ("revision", "name", "description", "manufacturer", "mpn")})
        for k in f:
            value = getattr(r, k)
            if value:
                f[k].setdefault(value, []).append(r.line)
        merged = lists.setdefault(occ.key, {"aml": set(), "identifiers": set()})
        merged["aml"].update(r.aml)
        merged["identifiers"].update(r.identifiers)

    def substitutes_of(r: Row) -> dict[str, int | None]:
        subs: dict[str, int | None] = {}
        for priority, sub_row in r.substitutes:
            sub = resolve(sub_row)
            if sub.kind == EXTERNAL:
                issue(ERROR, f"substitute {sub_row.pn} is mapped to an external workspace; substitutes must be own parts", sub_row.line)
            elif sub.kind == PART:
                record(sub)
                subs[sub.key] = priority
        return subs

    def visit(occ: _Occ) -> None:
        if occ.kind == IGNORED:
            below = _count(occ)
            plan.ignored_rows.append(occ.row.line)
            issue(INFO, f"{occ.row.pn} ignored by mapping" + (f" (and {below} row{'s' if below != 1 else ''} below it)" if below else ""), occ.row.line)
            return
        if occ.kind == EXTERNAL:
            below = _count(occ)
            if below:
                issue(
                    WARNING,
                    f"{below} row{'s' if below != 1 else ''} under {occ.row.pn} skipped: "
                    f"its breakdown comes from workspace {occ.key}",
                    occ.row.line,
                )
            return
        if occ.kind != PART:
            return
        if occ.row is not None:
            record(occ)
        if not occ.children:
            return
        agg: dict[str, dict[str, Any]] = {}
        for child in occ.children:
            if child.kind in (IGNORED, INVALID):
                continue
            r = child.row
            if r.quantity is None:
                what = "has no quantity" if not r.raw_quantity else f"quantity {r.raw_quantity!r} is not a positive number"
                issue(ERROR, f"{r.pn} {what}", r.line, f'fix the file, or: openbom map add "{r.pn}" --ignore')
                continue
            ck = ("ext:" if child.kind == EXTERNAL else "") + child.key
            uom = r.uom or DEFAULT_UOM
            subs = substitutes_of(r) if child.kind == PART else {}
            if ck in agg:
                a = agg[ck]
                if a["uom"] != uom:
                    issue(ERROR, f"{r.pn} is listed twice under one parent with units {a['uom']} and {uom}", r.line)
                    continue
                a["qty"] += Decimal(r.quantity)
                a["refdes"].update(r.refdes)
                a["subs"].update(subs)
                a["lines"].append(r.line)
                issue(WARNING, f"{r.pn} is listed twice under one parent (rows {a['lines'][0]}, {r.line}); quantities summed", r.line)
            else:
                agg[ck] = {
                    "qty": Decimal(r.quantity), "uom": uom, "refdes": set(r.refdes), "lines": [r.line],
                    "ext": child.kind == EXTERNAL, "key": child.key, "subs": dict(subs),
                }
        line = occ.row.line if occ.row else 0

        def sig(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
            return {
                k: (_fmt_qty(a["qty"]), a["uom"], sorted(a["refdes"]), sorted(a["subs"].items(), key=str))
                for k, a in entries.items()
            }

        signature = sig(agg)
        if occ.key in expansions:
            prev_line, prev = expansions[occ.key]
            prev_sig = sig(prev)
            if prev_sig != signature:
                issue(ERROR, f"{occ.key} is broken down differently here and at row {prev_line}", line or None)
        else:
            expansions[occ.key] = (line, agg)
        for child in occ.children:
            visit(child)

    if root_occ is not None:
        visit(root_occ)

    # 5. Reconcile parts with the org's current records.
    org = actor.org
    no_revision: list[str] = []
    for part_id, f in facts.items():
        line = first_line[part_id]
        values = {}
        for k, seen in f.items():
            if len(seen) > 1:
                detail = ", ".join(f"{v!r} (row {lines[0]})" for v, lines in seen.items())
                issue(ERROR, f"{part_id} has conflicting {k} values: {detail}", line)
            values[k] = next(iter(seen), None)
        current = store.get_part_node(org, part_id)
        try:
            if current is None:
                if not values["revision"]:
                    no_revision.append(part_id)
                node = make_node(
                    org=org,
                    part_id=part_id,
                    revision=values["revision"] or DEFAULT_REVISION,
                    name=values["name"] or part_id,
                    description=values["description"],
                    manufacturer=values["manufacturer"],
                    mpn=values["mpn"],
                    aml=lists[part_id]["aml"],
                    identifiers=lists[part_id]["identifiers"],
                )
                h = encode(node)[0]
                plan.parts.append(PartPlan(part_id, "new", line, node.revision, node.name, h, node=node))
                continue
            old = load_node(store, current)
            # TODO(question): empty cells keep the existing value rather than clearing it.
            node = make_node(
                org=org,
                part_id=part_id,
                revision=values["revision"] or old.revision,
                name=values["name"] or old.name,
                kind=old.kind,
                description=values["description"] or old.description,
                manufacturer=values["manufacturer"] or old.manufacturer,
                mpn=values["mpn"] or old.mpn,
                aml=lists[part_id]["aml"] or old.aml,
                identifiers=lists[part_id]["identifiers"] or old.identifiers,
                uris=old.uris,
                attributes=old.attributes,
            )
        except SchemaError as exc:
            issue(ERROR, f"{part_id}: {exc}", line)
            continue
        h = encode(node)[0]
        p = PartPlan(part_id, "unchanged", line, node.revision, node.name, h, old.revision, current, changed_fields(old, node), node)
        if h != current:
            p.action = "revise"
            if node.revision == old.revision:
                if update_metadata:
                    issue(WARNING, f"{part_id} rev {old.revision} updated in place ({', '.join(p.fields)})", line)
                else:
                    issue(
                        ERROR,
                        f"{part_id} rev {old.revision} in the file differs from the current record "
                        f"({', '.join(p.fields)}) but has the same revision label",
                        line,
                        "bump the revision in the source, or re-run with --update-metadata",
                    )
        plan.parts.append(p)
    if no_revision:
        listed = ", ".join(no_revision[:MAX_LISTED]) + ("…" if len(no_revision) > MAX_LISTED else "")
        issue(WARNING, f"{len(no_revision)} new parts have no revision and get '{DEFAULT_REVISION}': {listed}")
    plan.parts.sort(key=lambda p: p.part_id)

    # 6. Edges (external ones are pinned).
    for parent, (_, agg) in sorted(expansions.items()):
        for a in agg.values():
            refdes = sorted(a["refdes"])
            qty = _fmt_qty(a["qty"])
            if refdes and a["qty"] == a["qty"].to_integral_value() and len(refdes) != int(a["qty"]):
                issue(WARNING, f"{a['key']} under {parent}: {len(refdes)} reference designators but quantity {qty}", a["lines"][0])
            edge = EdgePlan(
                parent, a["key"], qty, a["uom"], refdes, a["lines"], external=a["ext"],
                substitutes=sorted(a["subs"].items(), key=lambda x: (x[1] is None, x[1] or 0, x[0])),
            )
            if edge.external:
                _pin(store, plan, edge, ws_id, issue)
            plan.edges.append(edge)

    if ws is not None:
        planned = {(e.parent, (ext_child_key if e.external else own_child_key)(e.child)) for e in plan.edges}
        dropped = [e for e in store.list_draft_edges(ws.id) if (e.parent_part_id, e.child_key) not in planned]
        plan.removed_draft_edges = len(dropped)
        if dropped:
            issue(WARNING, f"{len(dropped)} current draft edge(s) not in the file will be removed")

    if preview and not plan.errors:
        _preview(store, actor, plan)
    return plan


def _pin(store: Store, plan: ImportPlan, edge: EdgePlan, ws_id: str | None, issue: Any) -> None:
    source = edge.child
    if source == plan.workspace:
        issue(ERROR, f"workspace {source} cannot reference itself", edge.lines[0])
        return
    existing = store.get_draft_edge(ws_id, edge.parent, ext_child_key(source)) if ws_id else None
    if existing is not None:
        # Keep the current pin: moving it is `update`'s job, never a side effect of import.
        edge.pin_node, edge.pin_configuration, edge.pin_kept = existing.ext_node, existing.ext_configuration, True
        return
    try:
        _, comp_hash, comp = pinned_source(store, source)
    except OpenBomError as exc:
        issue(ERROR, f"cannot reference workspace {source}: {exc}", edge.lines[0])
        return
    edge.pin_node, edge.pin_configuration = comp.node, comp_hash


def _preview(store: Store, actor: Actor, plan: ImportPlan) -> None:
    """Apply in a rolled-back transaction and diff the resulting draft against HEAD."""
    with store.scratch():
        try:
            apply_plan(store, actor, plan)
            st = status(store, plan.workspace)
        except OpenBomError as exc:
            plan.issues.append(Issue(ERROR, f"cannot apply: {exc}"))
            return
    if st.build_error:
        plan.issues.append(Issue(ERROR, f"the resulting draft cannot be committed: {st.build_error}"))
    else:
        plan.preview = st.diff


@dataclass
class ApplyResult:
    parts_written: int
    edges: int
    removed_draft_edges: int
    created_workspace: bool


def apply_plan(store: Store, actor: Actor, plan: ImportPlan) -> ApplyResult:
    if plan.errors:
        raise OpenBomError(f"the import plan has {len(plan.errors)} error(s); fix them before applying")
    with store.transaction():
        written = 0
        for p in plan.parts:
            if p.action != "unchanged":
                save_node(store, actor, p.node)
                written += 1
        if plan.create_workspace:
            create_workspace(store, actor, plan.workspace, plan.root)
        edges = [
            DraftEdge(
                workspace_id=plan.workspace,
                parent_part_id=e.parent,
                child_key=ext_child_key(e.child) if e.external else own_child_key(e.child),
                child_part_id=None if e.external else e.child,
                ext_source_ws=e.child if e.external else None,
                ext_node=e.pin_node,
                ext_configuration=e.pin_configuration,
                quantity=e.quantity,
                uom=e.uom,
                refdes=tuple(e.refdes),
                substitutes=tuple(e.substitutes),
            )
            for e in plan.edges
        ]
        removed = replace_draft(store, actor, plan.workspace, edges)
    return ApplyResult(written, len(edges), removed, plan.create_workspace)


def run_import(store: Store, actor: Actor, path: str | Path, *, apply: bool = False, **kwargs: Any) -> tuple[ImportPlan, ApplyResult | None]:
    """Plan, and apply when asked and the plan has no errors."""
    plan = plan_import(store, actor, path, **kwargs)
    if apply and not plan.errors:
        return plan, apply_plan(store, actor, plan)
    return plan, None
