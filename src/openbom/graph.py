"""Graph helpers: typed object loading, part-ID adjacency (draft), and
traversal of committed configuration graphs.

Committed-graph functions take an ObjectSource (`get(hash) -> bytes | None`)
instead of a Store so they work over any set of objects (a DB, an in-memory
draft overlay, or later an offline bundle, D16).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping, Sequence, TypeVar

from .errors import NotFoundError, SchemaError
from .objects import Commit, Configuration, Correspondence, Entry, Node, decode, object_type

ObjectSource = Callable[[str], "bytes | None"]

T = TypeVar("T", Node, Configuration, Commit, Correspondence)


def load(get: ObjectSource, h: str, cls: type[T]) -> T | None:
    """Fetch and strictly decode an object of an expected type; None if absent."""
    data = get(h)
    if data is None:
        return None
    obj = decode(data)
    if not isinstance(obj, cls):
        raise SchemaError(f"object {h} is a {object_type(obj)}, expected {cls.__name__.lower()}")
    return obj


def require(get: ObjectSource, h: str, cls: type[T]) -> T:
    obj = load(get, h, cls)
    if obj is None:
        raise NotFoundError(f"{cls.__name__.lower()} {h} not found")
    return obj


def overlay(extra: Mapping[str, bytes], get: ObjectSource) -> ObjectSource:
    """An object source that sees `extra` (e.g., unsaved draft objects) first."""
    return lambda h: extra[h] if h in extra else get(h)


# --------------------------------------------------------------------------
# Part-ID adjacency (used by the draft build)
# --------------------------------------------------------------------------


def find_cycle(adj: Mapping[str, Sequence[str]], start: str) -> list[str] | None:
    """Return a cycle path [a, b, ..., a] reachable from start, or None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    path: list[str] = []
    # Iterative DFS: stack of (node, iterator over children).
    stack: list[tuple[str, Iterator[str]]] = [(start, iter(adj.get(start, ())))]
    color[start] = GRAY
    path.append(start)
    while stack:
        node, children = stack[-1]
        advanced = False
        for child in children:
            c = color.get(child, WHITE)
            if c == GRAY:
                return path[path.index(child):] + [child]
            if c == WHITE:
                color[child] = GRAY
                path.append(child)
                stack.append((child, iter(adj.get(child, ()))))
                advanced = True
                break
        if not advanced:
            color[node] = BLACK
            path.pop()
            stack.pop()
    return None


def postorder(adj: Mapping[str, Sequence[str]], start: str) -> list[str]:
    """Each node reachable from start once, children before parents. Assumes acyclic."""
    order: list[str] = []
    seen: set[str] = {start}
    stack: list[tuple[str, Iterator[str]]] = [(start, iter(adj.get(start, ())))]
    while stack:
        node, children = stack[-1]
        for child in children:
            if child not in seen:
                seen.add(child)
                stack.append((child, iter(adj.get(child, ()))))
                break
        else:
            order.append(node)
            stack.pop()
    return order


# --------------------------------------------------------------------------
# Committed configuration graphs
# --------------------------------------------------------------------------


def follows(entry: Entry, reveal: bool) -> bool:
    """Whether views traverse into this entry's configuration (§5 opacity rule)."""
    return entry.configuration is not None and (reveal or not entry.is_external)


@dataclass
class Walk:
    """Every configuration reachable from a root, each visited once."""

    configurations: dict[str, Configuration] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    # (parent configuration hash, entry) for external entries not traversed.
    opaque: list[tuple[str, Entry]] = field(default_factory=list)
    # (parent configuration hash, entry) for every external entry seen.
    externals: list[tuple[str, Entry]] = field(default_factory=list)


def walk(get: ObjectSource, root: str, *, reveal: bool = False) -> Walk:
    result = Walk()
    stack = [root]
    while stack:
        h = stack.pop()
        if h in result.configurations or h in result.missing:
            continue
        comp = load(get, h, Configuration)
        if comp is None:
            result.missing.append(h)
            continue
        result.configurations[h] = comp
        for e in comp.entries:
            if e.is_external:
                result.externals.append((h, e))
                if not reveal and e.configuration is not None:
                    result.opaque.append((h, e))
            if follows(e, reveal):
                stack.append(e.configuration)
    return result
