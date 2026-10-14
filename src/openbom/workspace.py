"""Workspaces: creation, draft edges, ownership checks, revision resolution,
and the commit build (§8.1).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .canonical import HASH_PREFIX
from .errors import ConflictError, CycleError, NotFoundError, NothingToCommit, OwnershipError, SchemaError
from .graph import find_cycle, postorder, require
from .identity import Actor, require_owner
from .objects import (
    DEFAULT_UOM,
    Commit,
    Configuration,
    encode,
    make_commit,
    make_configuration,
    make_entry,
    utc_timestamp,
    validate_quantity,
    validate_workspace_id,
)
from .parts import current_node, parse_part_ref, part_key
from .store import DraftEdge, Store, Workspace, ext_child_key, own_child_key

SECRET_BYTES = 32


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


def get_workspace(store: Store, ws_id: str) -> Workspace:
    ws = store.get_workspace(ws_id)
    if ws is None:
        raise NotFoundError(f"workspace '{ws_id}' does not exist")
    return ws


def owned_workspace(store: Store, actor: Actor, ws_id: str) -> Workspace:
    ws = get_workspace(store, ws_id)
    require_owner(actor, ws.owner_org_id, f"workspace '{ws_id}'")
    return ws


def create_workspace(store: Store, actor: Actor, ws_id: str, root_ref: str, name: str | None = None) -> Workspace:
    validate_workspace_id(ws_id)
    org, root_part_id = parse_part_ref(root_ref, actor.org)
    require_owner(actor, org, f"root part {part_key(org, root_part_id)}")
    with store.transaction():
        if store.get_workspace(ws_id) is not None:
            raise ConflictError(f"workspace '{ws_id}' already exists")
        current_node(store, org, root_part_id)
        ws = Workspace(
            id=ws_id,
            owner_org_id=actor.org,
            name=name or ws_id,
            root_part_id=root_part_id,
            secret=os.urandom(SECRET_BYTES),
            created_at=utc_timestamp(),
        )
        store.add_workspace(ws)
    return ws


def list_workspaces(store: Store) -> list[tuple[Workspace, str | None]]:
    return [(ws, store.get_ref(ws.id)) for ws in store.list_workspaces()]


# --------------------------------------------------------------------------
# Revisions (§8.6)
# --------------------------------------------------------------------------

_HEAD_RE = re.compile(r"HEAD(?:~([0-9]+))?")
_HEX_RE = re.compile(r"[0-9a-f]{8,64}")


def resolve_hash(store: Store, ref: str, obj_type: str | None = None) -> str:
    """Full hash or unique prefix (>= 8 hex chars, with or without 'sha256:')."""
    hexpart = ref[len(HASH_PREFIX):] if ref.startswith(HASH_PREFIX) else ref
    if not _HEX_RE.fullmatch(hexpart):
        raise SchemaError(
            f"{ref!r} is not a valid revision: use a hash, a unique prefix of at least 8 hex characters, "
            "HEAD, or HEAD~N"
        )
    matches = store.find_objects_by_prefix(HASH_PREFIX + hexpart, obj_type)
    what = obj_type or "object"
    if not matches:
        raise NotFoundError(f"no {what} matches {ref!r}")
    if len(matches) > 1:
        raise ConflictError(f"{ref!r} is ambiguous; it matches {len(matches)} {what}s")
    return matches[0]


def resolve_commit(store: Store, ws_id: str | None, rev: str) -> str:
    m = _HEAD_RE.fullmatch(rev)
    if m is None:
        return resolve_hash(store, rev, "commit")
    if ws_id is None:
        raise NotFoundError(f"{rev} needs a workspace; pass -w or run `ws use <ws_id>`")
    h = store.get_ref(get_workspace(store, ws_id).id)
    if h is None:
        raise NotFoundError(f"workspace '{ws_id}' has no commits yet")
    for i in range(int(m.group(1) or 0)):
        commit = require(store.get_object, h, Commit)
        if not commit.parents:
            raise NotFoundError(f"{rev}: workspace '{ws_id}' has only {i + 1} commit(s)")
        h = commit.parents[0]
    return h


def load_commit(store: Store, h: str) -> Commit:
    return require(store.get_object, h, Commit)


def head(store: Store, ws_id: str) -> tuple[str, Commit] | None:
    h = store.get_ref(ws_id)
    return None if h is None else (h, load_commit(store, h))


def log(store: Store, ws_id: str, limit: int | None = None) -> list[tuple[str, Commit]]:
    entries: list[tuple[str, Commit]] = []
    h = store.get_ref(get_workspace(store, ws_id).id)
    while h is not None and (limit is None or len(entries) < limit):
        commit = load_commit(store, h)
        entries.append((h, commit))
        h = commit.parents[0] if commit.parents else None
    return entries


def history_configurations(store: Store, ws_id: str) -> set[str] | None:
    """Root configuration hashes of every commit on a workspace's main; None if no such workspace."""
    if store.get_workspace(ws_id) is None:
        return None
    return {c.configuration for _, c in log(store, ws_id)}


# --------------------------------------------------------------------------
# Draft edges
# --------------------------------------------------------------------------


def _own_part(store: Store, actor: Actor, ref: str, role: str) -> str:
    org, part_id = parse_part_ref(ref, actor.org)
    if org != actor.org:
        raise OwnershipError(
            f"{role} part {part_key(org, part_id)} is owned by org '{org}'; you are acting as '{actor.org}'. "
            "Reference another workspace's assembly with `edge add-external`."
        )
    current_node(store, org, part_id)
    return part_id


def normalize_child_key(key: str, owner_org: str) -> str:
    """CLI child keys: `<part_id>`, `<org>:<part_id>` (own org), `part:<part_id>`, or `ext:<ws>`."""
    if key.startswith("ext:"):
        return ext_child_key(validate_workspace_id(key[4:]))
    if key.startswith("part:"):
        key = key[5:]
    org, part_id = parse_part_ref(key, owner_org)
    if org != owner_org:
        raise SchemaError(f"child key {key!r}: own children belong to org '{owner_org}'")
    return own_child_key(part_id)


def _describe_edge(org: str, edge: DraftEdge) -> str:
    child = part_key(org, edge.child_part_id) if not edge.is_external else edge.child_key
    return f"{part_key(org, edge.parent_part_id)} -> {child}"


def _edge_fields(quantity: str, uom: str, refdes: Iterable[str]) -> tuple[str, str, tuple[str, ...]]:
    validate_quantity(quantity)
    if not uom:
        raise SchemaError("uom must be a non-empty string")
    return quantity, uom, tuple(sorted(set(refdes)))


def add_edge(
    store: Store,
    actor: Actor,
    ws_id: str,
    parent: str,
    child: str,
    *,
    quantity: str,
    uom: str = DEFAULT_UOM,
    refdes: Iterable[str] = (),
) -> DraftEdge:
    quantity, uom, refdes = _edge_fields(quantity, uom, refdes)
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        parent_id = _own_part(store, actor, parent, "parent")
        child_id = _own_part(store, actor, child, "child")
        edge = DraftEdge(
            workspace_id=ws.id,
            parent_part_id=parent_id,
            child_key=own_child_key(child_id),
            child_part_id=child_id,
            quantity=quantity,
            uom=uom,
            refdes=refdes,
        )
        if store.get_draft_edge(ws.id, parent_id, edge.child_key) is not None:
            raise ConflictError(f"edge {_describe_edge(ws.owner_org_id, edge)} already exists; use `edge set`")
        store.put_draft_edge(edge)
    return edge


def pinned_source(store: Store, source_ws: str, rev: str | None = None) -> tuple[str, str, Configuration]:
    """(commit hash, root configuration hash, configuration) of a source workspace revision."""
    commit_hash = resolve_commit(store, source_ws, rev or "HEAD")
    commit = load_commit(store, commit_hash)
    if commit.workspace != source_ws:
        raise SchemaError(f"commit {commit_hash} belongs to workspace '{commit.workspace}', not '{source_ws}'")
    return commit_hash, commit.configuration, require(store.get_object, commit.configuration, Configuration)


def add_external_edge(
    store: Store,
    actor: Actor,
    ws_id: str,
    parent: str,
    source_ws: str,
    *,
    quantity: str,
    uom: str = DEFAULT_UOM,
    refdes: Iterable[str] = (),
    rev: str | None = None,
) -> DraftEdge:
    quantity, uom, refdes = _edge_fields(quantity, uom, refdes)
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        parent_id = _own_part(store, actor, parent, "parent")
        if source_ws == ws.id:
            raise ConflictError("a workspace cannot reference itself as an external source")
        get_workspace(store, source_ws)
        _, comp_hash, comp = pinned_source(store, source_ws, rev)
        edge = DraftEdge(
            workspace_id=ws.id,
            parent_part_id=parent_id,
            child_key=ext_child_key(source_ws),
            ext_source_ws=source_ws,
            ext_node=comp.node,
            ext_configuration=comp_hash,
            quantity=quantity,
            uom=uom,
            refdes=refdes,
        )
        if store.get_draft_edge(ws.id, parent_id, edge.child_key) is not None:
            raise ConflictError(f"edge {_describe_edge(ws.owner_org_id, edge)} already exists; use `edge set`")
        store.put_draft_edge(edge)
    return edge


def _existing_edge(store: Store, ws: Workspace, parent: str, child_key: str) -> DraftEdge:
    org, parent_id = parse_part_ref(parent, ws.owner_org_id)
    key = normalize_child_key(child_key, ws.owner_org_id)
    edge = store.get_draft_edge(ws.id, parent_id, key)
    if edge is None:
        raise NotFoundError(f"no draft edge {part_key(org, parent_id)} -> {key} in workspace '{ws.id}'")
    return edge


def set_edge(
    store: Store,
    actor: Actor,
    ws_id: str,
    parent: str,
    child_key: str,
    *,
    quantity: str | None = None,
    uom: str | None = None,
    refdes: Iterable[str] | None = None,
) -> DraftEdge:
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        old = _existing_edge(store, ws, parent, child_key)
        quantity, uom, refdes = _edge_fields(
            quantity if quantity is not None else old.quantity,
            uom if uom is not None else old.uom,
            refdes if refdes is not None else old.refdes,
        )
        edge = DraftEdge(**{**old.__dict__, "quantity": quantity, "uom": uom, "refdes": refdes})
        store.put_draft_edge(edge)
    return edge


def remove_edge(store: Store, actor: Actor, ws_id: str, parent: str, child_key: str) -> DraftEdge:
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        edge = _existing_edge(store, ws, parent, child_key)
        store.delete_draft_edge(ws.id, edge.parent_part_id, edge.child_key)
    return edge


def replace_draft(store: Store, actor: Actor, ws_id: str, edges: Iterable[DraftEdge]) -> int:
    """Replace a workspace's whole draft (used by import). Returns the number of edges removed."""
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        removed = store.delete_all_draft_edges(ws.id)
        for edge in edges:
            if edge.workspace_id != ws.id:
                raise SchemaError(f"edge for workspace '{edge.workspace_id}' cannot go into '{ws.id}'")
            _edge_fields(edge.quantity, edge.uom, edge.refdes)
            store.put_draft_edge(edge)
    return removed


def repin_external_edge(store: Store, edge: DraftEdge, node: str, configuration: str) -> DraftEdge:
    """Move an external edge's pin (used by sync.update; caller checks ownership)."""
    new = DraftEdge(**{**edge.__dict__, "ext_node": node, "ext_configuration": configuration})
    store.put_draft_edge(new)
    return new


# --------------------------------------------------------------------------
# Commit build (§8.1)
# --------------------------------------------------------------------------


@dataclass
class DraftBuild:
    root: str
    # Configuration objects built from the draft: hash -> canonical bytes. Not yet stored.
    objects: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def build_draft(store: Store, ws: Workspace) -> DraftBuild:
    """Turn the draft into configuration objects, bottom-up, without storing them."""
    org = ws.owner_org_id
    root = ws.root_part_id
    edges = store.list_draft_edges(ws.id)
    by_parent: dict[str, list[DraftEdge]] = defaultdict(list)
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        by_parent[e.parent_part_id].append(e)
        if not e.is_external:
            adj[e.parent_part_id].append(e.child_part_id)

    cycle = find_cycle(adj, root)
    if cycle is not None:
        raise CycleError("cycle detected: " + " -> ".join(part_key(org, p) for p in cycle))

    order = postorder(adj, root)
    reachable = set(order)
    warnings = [
        f"edge {_describe_edge(org, e)} is not reachable from root {part_key(org, root)}; it will not be committed"
        for e in edges
        if e.parent_part_id not in reachable
    ]

    nodes: dict[str, str] = {}
    for p in order:
        try:
            nodes[p] = current_node(store, org, p)
        except NotFoundError as exc:
            raise NotFoundError(f"draft edge references a missing part: {exc}") from None

    comps: dict[str, str] = {}
    objects: dict[str, bytes] = {}
    for p in order:  # post-order: children are built before parents; each part once (DAG memo).
        entries = []
        for e in by_parent.get(p, ()):
            if e.is_external:
                entries.append(
                    make_entry(
                        node=e.ext_node,
                        configuration=e.ext_configuration,
                        source=e.ext_source_ws,
                        quantity=e.quantity,
                        uom=e.uom,
                        refdes=e.refdes,
                    )
                )
            else:
                entries.append(
                    make_entry(
                        node=nodes[e.child_part_id],
                        configuration=comps.get(e.child_part_id),
                        quantity=e.quantity,
                        uom=e.uom,
                        refdes=e.refdes,
                        substitutes=[(_substitute_node(store, org, sub, e), prio) for sub, prio in e.substitutes],
                    )
                )
        if entries or p == root:
            h, data = encode(make_configuration(node=nodes[p], entries=entries, secret=ws.secret))
            comps[p] = h
            objects[h] = data
    return DraftBuild(root=comps[root], objects=objects, warnings=warnings)


def _substitute_node(store: Store, org: str, part_id: str, edge: DraftEdge) -> str:
    try:
        return current_node(store, org, part_id)
    except NotFoundError:
        raise NotFoundError(
            f"edge {_describe_edge(org, edge)} lists substitute {part_key(org, part_id)}, which does not exist"
        ) from None


@dataclass
class CommitResult:
    hash: str
    commit: Commit
    warnings: list[str]


def commit(store: Store, actor: Actor, ws_id: str, message: str) -> CommitResult:
    if store.get_reconciliation(ws_id) is not None:
        raise ConflictError(f"workspace '{ws_id}' is a reconciliation; use `reconcile commit`")
    return commit_draft(store, actor, ws_id, message)


def commit_draft(
    store: Store,
    actor: Actor,
    ws_id: str,
    message: str,
    *,
    correspondence: tuple[str, bytes] | None = None,
) -> CommitResult:
    """Build the draft and commit it, optionally recording a correspondence object (reconciliation)."""
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        build = build_draft(store, ws)
        current = head(store, ws.id)
        if (
            current is not None
            and current[1].configuration == build.root
            and current[1].correspondence == (correspondence[0] if correspondence else None)
        ):
            raise NothingToCommit("nothing to commit (draft matches HEAD)")
        for data in build.objects.values():
            store.put_object("configuration", ws.owner_org_id, data)
        if correspondence is not None:
            store.put_object("correspondence", ws.owner_org_id, correspondence[1])
        new = make_commit(
            workspace=ws.id,
            configuration=build.root,
            parents=[current[0]] if current else [],
            author_org=actor.org,
            author_user=actor.user,
            message=message,
            correspondence=correspondence[0] if correspondence else None,
        )
        h, data = encode(new)
        store.put_object("commit", actor.org, data)
        store.set_ref(ws.id, h)
    return CommitResult(hash=h, commit=new, warnings=build.warnings)
