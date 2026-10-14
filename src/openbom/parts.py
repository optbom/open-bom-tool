"""Parts: stable `org:part_id` identities whose revisions are immutable node objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import ConflictError, NotFoundError, SchemaError
from .graph import require
from .identity import Actor, require_owner
from .objects import Node, encode, make_node, utc_timestamp, validate_org_id, validate_part_id
from .store import Store


def parse_part_ref(ref: str, default_org: str | None) -> tuple[str, str]:
    """`org:part_id` or bare `part_id` (resolved against default_org)."""
    if ":" in ref:
        org, part_id = ref.split(":", 1)
        return validate_org_id(org), validate_part_id(part_id)
    if default_org is None:
        raise SchemaError(f"part reference {ref!r} needs an org prefix (org:part_id) when no org is acting")
    return default_org, validate_part_id(ref)


def part_key(org: str, part_id: str) -> str:
    return f"{org}:{part_id}"


def load_node(store: Store, h: str) -> Node:
    return require(store.get_object, h, Node)


def current_node(store: Store, org: str, part_id: str) -> str:
    h = store.get_part_node(org, part_id)
    if h is None:
        raise NotFoundError(f"part {part_key(org, part_id)} does not exist")
    return h


def _put_node(store: Store, node: Node) -> str:
    h, data = encode(node)
    store.put_object("node", node.org, data)
    store.set_part_node(node.org, node.part_id, h, utc_timestamp())
    return h


def save_node(store: Store, actor: Actor, node: Node) -> str:
    """Make a fully built node the current revision of its part (used by import)."""
    require_owner(actor, node.org, f"part {node.key}")
    with store.transaction():
        return _put_node(store, node)


def add_part(
    store: Store,
    actor: Actor,
    part_ref: str,
    *,
    revision: str,
    name: str,
    description: str | None = None,
    manufacturer: str | None = None,
    mpn: str | None = None,
    identifiers: Iterable[tuple[str, str]] = (),
    uris: Iterable[str] = (),
    attributes: Mapping[str, str] | None = None,
) -> tuple[str, Node]:
    org, part_id = parse_part_ref(part_ref, actor.org)
    require_owner(actor, org, f"part {part_key(org, part_id)}")
    node = make_node(
        org=org,
        part_id=part_id,
        revision=revision,
        name=name,
        description=description,
        manufacturer=manufacturer,
        mpn=mpn,
        identifiers=identifiers,
        uris=uris,
        attributes=attributes,
    )
    with store.transaction():
        if store.get_part_node(org, part_id) is not None:
            raise ConflictError(f"part {node.key} already exists; use `part revise`")
        return _put_node(store, node), node


def revise_part(
    store: Store,
    actor: Actor,
    part_ref: str,
    *,
    revision: str,
    name: str | None = None,
    description: str | None = None,
    manufacturer: str | None = None,
    mpn: str | None = None,
    identifiers: Iterable[tuple[str, str]] | None = None,
    uris: Iterable[str] | None = None,
    attributes: Mapping[str, str] | None = None,
) -> tuple[str, Node]:
    """Create a new revision. Fields left as None carry over from the current revision.

    TODO(spec): list/map fields (identifiers, uris, attributes) are replaced
    wholesale when given, not merged.
    TODO(spec): reusing the same revision label is allowed (e.g., a metadata
    fix); only a byte-identical node is rejected.
    """
    org, part_id = parse_part_ref(part_ref, actor.org)
    require_owner(actor, org, f"part {part_key(org, part_id)}")
    with store.transaction():
        old_hash = current_node(store, org, part_id)
        old = load_node(store, old_hash)
        node = make_node(
            org=org,
            part_id=part_id,
            revision=revision,
            name=name if name is not None else old.name,
            kind=old.kind,
            description=description if description is not None else old.description,
            manufacturer=manufacturer if manufacturer is not None else old.manufacturer,
            mpn=mpn if mpn is not None else old.mpn,
            identifiers=identifiers if identifiers is not None else old.identifiers,
            uris=uris if uris is not None else old.uris,
            attributes=attributes if attributes is not None else old.attributes,
        )
        if encode(node)[0] == old_hash:
            raise ConflictError(f"part {node.key} revision would be identical to the current one")
        return _put_node(store, node), node


@dataclass(frozen=True)
class PartRevision:
    node_hash: str
    node: Node
    created_at: str
    current: bool


def part_history(store: Store, org: str, part_id: str) -> list[PartRevision]:
    current = current_node(store, org, part_id)
    return [
        PartRevision(r["node"], load_node(store, r["node"]), r["created_at"], r["node"] == current)
        for r in store.part_revisions(org, part_id)
    ]


def list_parts(store: Store, org: str | None = None) -> list[tuple[str, Node]]:
    return [(r["current_node"], load_node(store, r["current_node"])) for r in store.list_parts(org)]
