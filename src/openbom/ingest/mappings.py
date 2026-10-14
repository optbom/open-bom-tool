"""Persisted import decisions: what a part number in an imported file means.

A mapping belongs to the acting org and applies to every later import, so each
decision is made once. Targets:
- part:     rename to an own part ID (e.g., a PN with characters part IDs don't allow)
- external: the row is another workspace's assembly, referenced by pinned hash
- ignore:   drop the row and everything under it (e.g., "as required" consumables)
"""

from __future__ import annotations

from ..errors import NotFoundError, OpenBomError
from ..identity import Actor
from ..objects import validate_part_id
from ..store import Mapping, Store
from ..workspace import get_workspace


def set_mapping(
    store: Store,
    actor: Actor,
    source_pn: str,
    *,
    part_id: str | None = None,
    ext_source_ws: str | None = None,
    ignore: bool = False,
) -> Mapping:
    if sum([part_id is not None, ext_source_ws is not None, ignore]) != 1:
        raise OpenBomError("choose exactly one of --part, --from, or --ignore")
    if not source_pn.strip():
        raise OpenBomError("source part number cannot be empty")
    if part_id is not None:
        validate_part_id(part_id)
    if ext_source_ws is not None:
        get_workspace(store, ext_source_ws)
    mapping = Mapping(
        org_id=actor.org, source_pn=source_pn.strip(), part_id=part_id, ext_source_ws=ext_source_ws, ignore=ignore
    )
    with store.transaction():
        store.put_mapping(mapping)
    return mapping


def remove_mapping(store: Store, actor: Actor, source_pn: str) -> None:
    with store.transaction():
        if not store.delete_mapping(actor.org, source_pn):
            raise NotFoundError(f"no mapping for {source_pn!r} in org '{actor.org}'")


def list_mappings(store: Store, actor: Actor) -> list[Mapping]:
    return store.list_mappings(actor.org)


def describe(mapping: Mapping) -> str:
    if mapping.ignore:
        return "ignore"
    if mapping.ext_source_ws:
        return f"external {mapping.ext_source_ws}"
    return f"part {mapping.org_id}:{mapping.part_id}"
