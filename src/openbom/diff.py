"""Computed diffs between two configuration graphs (§8.3). Commits never store diffs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .canonical import short_hash
from .graph import ObjectSource, load, walk
from .objects import NODE_DESCRIPTIVE_FIELDS, Node


@dataclass(frozen=True)
class EdgeInfo:
    quantity: str
    uom: str
    refdes: tuple[str, ...]
    configuration: str | None
    source: str | None
    substitutes: tuple[str, ...] = ()  # substitute part keys, in priority order


@dataclass
class Flat:
    parts: dict[str, str] = field(default_factory=dict)  # "org:part_id" -> node hash
    nodes: dict[str, Node] = field(default_factory=dict)  # node hash -> node
    edges: dict[tuple[str, str], EdgeInfo] = field(default_factory=dict)  # (parent key, child key)
    missing: list[str] = field(default_factory=list)


def _node_key(get: ObjectSource, flat: Flat, h: str) -> str:
    node = flat.nodes.get(h) or load(get, h, Node)
    if node is None:
        flat.missing.append(h)
        return f"<missing node {short_hash(h)}>"
    flat.nodes[h] = node
    # TODO(spec): with --reveal, another org's breakdown could in principle use a
    # different revision of the same org:part_id; first occurrence wins here.
    flat.parts.setdefault(node.key, h)
    return node.key


def flatten(get: ObjectSource, root: str | None, *, reveal: bool = False) -> Flat:
    """Parts and edges reachable from a root configuration (not crossing into
    external configurations unless reveal)."""
    flat = Flat()
    if root is None:
        return flat
    w = walk(get, root, reveal=reveal)
    flat.missing.extend(w.missing)
    for comp in w.configurations.values():
        parent = _node_key(get, flat, comp.node)
        for e in comp.entries:
            child = _node_key(get, flat, e.node)
            subs = tuple(_node_key(get, flat, n) for n, _ in e.substitutes)
            flat.edges[(parent, child)] = EdgeInfo(e.quantity, e.uom, e.refdes, e.configuration, e.source, subs)
    return flat


@dataclass
class PartRef:
    part: str
    node: str
    revision: str | None


@dataclass
class PartChange:
    part: str
    old_node: str
    new_node: str
    old_revision: str | None
    new_revision: str | None
    fields: list[str]


@dataclass
class EdgeChange:
    parent: str
    child: str
    change: str  # "added" | "removed" | "changed"
    fields: list[str]
    old: EdgeInfo | None
    new: EdgeInfo | None


@dataclass
class OpaqueChange:
    parent: str
    child: str
    source: str | None
    old_configuration: str | None
    new_configuration: str | None


@dataclass
class DiffResult:
    added_parts: list[PartRef] = field(default_factory=list)
    removed_parts: list[PartRef] = field(default_factory=list)
    revised_parts: list[PartChange] = field(default_factory=list)
    edge_changes: list[EdgeChange] = field(default_factory=list)
    opaque_changes: list[OpaqueChange] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_parts or self.removed_parts or self.revised_parts or self.edge_changes or self.opaque_changes
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def changed_fields(old: Node, new: Node) -> list[str]:
    ob, nb = old.body(), new.body()
    fields = [f for f in NODE_DESCRIPTIVE_FIELDS if ob.get(f) != nb.get(f)]
    oa, na = old.attributes, new.attributes
    fields += [f"attributes.{k}" for k in sorted(oa.keys() | na.keys()) if oa.get(k) != na.get(k)]
    return fields


def _revision(flat: Flat, h: str) -> str | None:
    node = flat.nodes.get(h)
    return node.revision if node else None


def diff_configurations(get: ObjectSource, a: str | None, b: str | None, *, reveal: bool = False) -> DiffResult:
    """Diff two root configurations (None means empty).

    TODO(spec): §8.3 says unchanged subtrees are skipped without traversal. We
    traverse each side once (linear, DAG-memoized) because an exact part set is
    needed for added/removed parts; identical subtrees produce identical
    edges and so drop out of the comparison. Pairwise pruning is a D17 concern.
    """
    result = DiffResult()
    if a == b:
        return result
    fa, fb = flatten(get, a, reveal=reveal), flatten(get, b, reveal=reveal)
    result.missing = sorted(set(fa.missing) | set(fb.missing))

    for key in sorted(fb.parts.keys() - fa.parts.keys()):
        result.added_parts.append(PartRef(key, fb.parts[key], _revision(fb, fb.parts[key])))
    for key in sorted(fa.parts.keys() - fb.parts.keys()):
        result.removed_parts.append(PartRef(key, fa.parts[key], _revision(fa, fa.parts[key])))
    for key in sorted(fa.parts.keys() & fb.parts.keys()):
        old_h, new_h = fa.parts[key], fb.parts[key]
        if old_h != new_h:
            old, new = fa.nodes.get(old_h), fb.nodes.get(new_h)
            result.revised_parts.append(
                PartChange(
                    part=key,
                    old_node=old_h,
                    new_node=new_h,
                    old_revision=old.revision if old else None,
                    new_revision=new.revision if new else None,
                    fields=changed_fields(old, new) if old and new else [],
                )
            )

    for key in sorted(fa.edges.keys() | fb.edges.keys()):
        old, new = fa.edges.get(key), fb.edges.get(key)
        parent, child = key
        if old is None or new is None:
            change = "added" if old is None else "removed"
            result.edge_changes.append(EdgeChange(parent, child, change, [], old, new))
            continue
        fields = [
            f for f in ("quantity", "uom", "refdes", "substitutes", "source") if getattr(old, f) != getattr(new, f)
        ]
        if fields:
            result.edge_changes.append(EdgeChange(parent, child, "changed", fields, old, new))
        external = old.source is not None or new.source is not None
        if external and not reveal and old.configuration != new.configuration:
            result.opaque_changes.append(
                OpaqueChange(parent, child, new.source or old.source, old.configuration, new.configuration)
            )
    return result
