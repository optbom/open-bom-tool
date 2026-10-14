"""Cross-workspace propagation (§6, §8.4): pinned external entries are reported
by `outdated` and moved forward explicitly by `update`.

`update` is a plain library function so a future opt-in policy engine (D19)
can call it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import NotFoundError, OpenBomError
from .graph import load, require, walk
from .identity import Actor
from .objects import Configuration, Node
from .parts import part_key
from .store import Store
from .workspace import head, owned_workspace, pinned_source, repin_external_edge

OUTDATED = "outdated"
UP_TO_DATE = "up_to_date"
SOURCE_NOT_FOUND = "source_not_found"
NO_COMMITS = "source_has_no_commits"


@dataclass
class ExternalStatus:
    parent: str  # parent "org:part_id"
    part: str  # pinned child "org:part_id"
    source: str
    status: str
    pinned_node: str
    pinned_revision: str | None
    pinned_configuration: str | None
    latest_node: str | None = None
    latest_revision: str | None = None
    latest_configuration: str | None = None


def _node(store: Store, h: str | None) -> Node | None:
    return None if h is None else load(store.get_object, h, Node)


def check_external(store: Store, parent: str, source: str, node: str, configuration: str | None) -> ExternalStatus:
    pinned = _node(store, node)
    status = ExternalStatus(
        parent=parent,
        part=pinned.key if pinned else f"<missing node {node}>",
        source=source,
        status=UP_TO_DATE,
        pinned_node=node,
        pinned_revision=pinned.revision if pinned else None,
        pinned_configuration=configuration,
    )
    if store.get_workspace(source) is None:
        status.status = SOURCE_NOT_FOUND
        return status
    current = head(store, source)
    if current is None:
        status.status = NO_COMMITS
        return status
    latest_comp = current[1].configuration
    comp = require(store.get_object, latest_comp, Configuration)
    latest = _node(store, comp.node)
    status.latest_node = comp.node
    status.latest_revision = latest.revision if latest else None
    status.latest_configuration = latest_comp
    if latest_comp != configuration:
        status.status = OUTDATED
    return status


def outdated(store: Store, ws_id: str) -> list[ExternalStatus]:
    """External entries reachable from HEAD (own configurations only) that aren't current."""
    current = head(store, ws_id)
    if current is None:
        return []
    w = walk(store.get_object, current[1].configuration, reveal=False)
    results = []
    seen = set()
    for comp_hash, e in w.externals:
        parent_node = _node(store, w.configurations[comp_hash].node)
        parent = parent_node.key if parent_node else comp_hash
        key = (parent, e.source, e.node, e.configuration)
        if key in seen:
            continue
        seen.add(key)
        status = check_external(store, parent, e.source, e.node, e.configuration)
        if status.status != UP_TO_DATE:
            results.append(status)
    return results


def draft_externals(store: Store, ws_id: str) -> list[ExternalStatus]:
    """Status of every external edge in the draft (used by `status`)."""
    ws = store.get_workspace(ws_id)
    if ws is None:
        raise NotFoundError(f"workspace '{ws_id}' does not exist")
    return [
        check_external(
            store, part_key(ws.owner_org_id, e.parent_part_id), e.ext_source_ws, e.ext_node, e.ext_configuration
        )
        for e in store.list_draft_edges(ws_id)
        if e.is_external
    ]


@dataclass
class UpdateResult:
    updated: list[ExternalStatus]  # status before the update (pinned -> latest)
    skipped: list[ExternalStatus]  # not updatable (source missing / no commits)


def update(store: Store, actor: Actor, ws_id: str, part_ref: str | None = None, *, all: bool = False) -> UpdateResult:
    """Move matching draft external edges to their source's latest commit. Does not commit."""
    if (part_ref is None) == (not all):
        raise OpenBomError("pass either a part (org:part_id) or --all")
    result = UpdateResult(updated=[], skipped=[])
    with store.transaction():
        ws = owned_workspace(store, actor, ws_id)
        matched = False
        for edge in store.list_draft_edges(ws.id):
            if not edge.is_external:
                continue
            parent = part_key(ws.owner_org_id, edge.parent_part_id)
            status = check_external(store, parent, edge.ext_source_ws, edge.ext_node, edge.ext_configuration)
            if part_ref is not None:
                # `org:part_id` matches exactly; a bare part_id matches any org.
                target = status.part if ":" in part_ref else status.part.split(":", 1)[-1]
                if target != part_ref:
                    continue
            matched = True
            if status.status in (SOURCE_NOT_FOUND, NO_COMMITS):
                result.skipped.append(status)
            elif status.status == OUTDATED:
                _, comp_hash, comp = pinned_source(store, edge.ext_source_ws)
                repin_external_edge(store, edge, comp.node, comp_hash)
                result.updated.append(status)
        if part_ref is not None and not matched:
            raise NotFoundError(f"no external entry for {part_ref} in the draft of workspace '{ws.id}'")
    return result
