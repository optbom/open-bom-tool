"""Indented-tree representation of a configuration (§8.2).

build_tree produces a nested, JSON-serializable structure (used for --json);
render_text turns it into the CLI tree.
"""

from __future__ import annotations

from typing import Any

from ..canonical import short_hash
from ..graph import ObjectSource, follows, load
from ..objects import Configuration, Entry, Node
from .refdes import compact_refdes

# Status of each tree item's configuration.
LEAF = "leaf"  # no configuration: a bought part with no known structure
EXPANDED = "expanded"
OPAQUE = "opaque"  # external, not revealed
SEE_ABOVE = "see_above"  # shared sub-assembly already rendered
MISSING = "missing"  # pinned object not in the store
TRUNCATED = "truncated"  # beyond --depth


def _node_fields(get: ObjectSource, h: str) -> dict[str, Any]:
    node = load(get, h, Node)
    if node is None:
        return {"node": h, "node_missing": True}
    item: dict[str, Any] = {
        "node": h,
        "part": node.key,
        "revision": node.revision,
        "name": node.name,
    }
    if node.manufacturer:
        item["manufacturer"] = node.manufacturer
    if node.mpn:
        item["mpn"] = node.mpn
    if node.aml:
        item["aml"] = [{"manufacturer": m, "mpn": p} for m, p in node.aml]
    return item


def build_tree(get: ObjectSource, root: str, *, reveal: bool = False, max_depth: int | None = None) -> dict[str, Any]:
    comp = load(get, root, Configuration)
    if comp is None:
        return {"configuration": root, "status": MISSING, "children": []}
    tree = _node_fields(get, comp.node)
    tree["configuration"] = root
    seen = {root}
    if max_depth is not None and max_depth <= 0:
        tree.update(status=TRUNCATED, children=[])
    else:
        tree.update(status=EXPANDED, children=_children(get, comp, 1, reveal, max_depth, seen))
    return tree


def _children(
    get: ObjectSource, comp: Configuration, depth: int, reveal: bool, max_depth: int | None, seen: set[str]
) -> list[dict[str, Any]]:
    # Entries are stored sorted by hash; display them by part key instead.
    # TODO(spec): the spec doesn't define a display order.
    def display_key(e: Entry) -> tuple[str, str]:
        node = load(get, e.node, Node)
        return (node.key if node else "~", e.source or "")

    return [_item(get, e, depth, reveal, max_depth, seen) for e in sorted(comp.entries, key=display_key)]


def _item(
    get: ObjectSource, e: Entry, depth: int, reveal: bool, max_depth: int | None, seen: set[str]
) -> dict[str, Any]:
    item = _node_fields(get, e.node)
    item.update(quantity=e.quantity, uom=e.uom)
    if e.refdes:
        item["refdes"] = list(e.refdes)
    if e.substitutes:
        item["substitutes"] = []
        for sub, priority in e.substitutes:
            fields = _node_fields(get, sub)
            item["substitutes"].append({"node": sub, "part": fields.get("part"), "revision": fields.get("revision"), "priority": priority})
    if e.source:
        item["source"] = e.source
    item["configuration"] = e.configuration
    item["children"] = []
    if e.configuration is None:
        item["status"] = LEAF
    elif not follows(e, reveal):
        item["status"] = OPAQUE
    elif e.configuration in seen:
        item["status"] = SEE_ABOVE
    elif max_depth is not None and depth >= max_depth:
        item["status"] = TRUNCATED
    else:
        child = load(get, e.configuration, Configuration)
        if child is None:
            item["status"] = MISSING
        else:
            seen.add(e.configuration)
            item["status"] = EXPANDED
            item["children"] = _children(get, child, depth + 1, reveal, max_depth, seen)
    return item


def _fmt_hash(h: str, full: bool) -> str:
    return h if full else short_hash(h) + "…"


def _label(item: dict[str, Any], full: bool) -> str:
    parts = []
    if "quantity" in item:
        parts.append(f"{item['quantity']} {item['uom']}")
    if item.get("node_missing"):
        parts.append(f"<node {_fmt_hash(item['node'], full)}>  [missing]")
    else:
        parts.append(f"{item['part']} rev {item['revision']}")
        parts.append(f'"{item["name"]}"')
    extras = []
    if item.get("manufacturer"):
        extras.append(f"mfr: {item['manufacturer']}")
    if item.get("mpn"):
        extras.append(f"mpn: {item['mpn']}")
    approved = len(item.get("aml", []))
    if approved > 1 or (approved == 1 and not item.get("mpn")):
        extras.append(f"aml: {approved} approved")
    if item.get("refdes"):
        extras.append(f"refdes: {compact_refdes(item['refdes'])}")
    if extras:
        parts.append(f"({', '.join(extras)})")
    if item.get("source"):
        parts.append(f"⟵ {item['source']}")
    status = item.get("status")
    if item.get("configuration") and status in (EXPANDED, SEE_ABOVE, TRUNCATED):
        parts.append(f"[{_fmt_hash(item['configuration'], full)}]")
    if full and not item.get("node_missing"):
        parts.append(f"node {item['node']}")
    if status == SEE_ABOVE:
        parts.append("(see above)")
    elif status == TRUNCATED and "quantity" in item:
        parts.append("…")
    return "  ".join(parts)


def render_text(tree: dict[str, Any], *, full_hashes: bool = False) -> str:
    if tree.get("status") == MISSING and "part" not in tree and not tree.get("node_missing"):
        return f"configuration {tree['configuration']}  [missing]"
    lines = [_label(tree, full_hashes)]

    def emit(children: list[dict[str, Any]], prefix: str) -> None:
        for i, child in enumerate(children):
            last = i == len(children) - 1
            lines.append(prefix + ("└── " if last else "├── ") + _label(child, full_hashes))
            cont = prefix + ("    " if last else "│   ")
            if child.get("substitutes"):
                subs = ", ".join(
                    f"{x['part'] or _fmt_hash(x['node'], full_hashes)}" + (f" rev {x['revision']}" if x["revision"] else "")
                    for x in child["substitutes"]
                )
                lines.append(f"{cont}      or substitute: {subs}")
            if child["status"] in (OPAQUE, MISSING):
                tag = "[opaque]" if child["status"] == OPAQUE else "[missing]"
                lines.append(f"{cont}      configuration {_fmt_hash(child['configuration'], full_hashes)}  {tag}")
            emit(child["children"], cont)

    emit(tree.get("children", []), "")
    return "\n".join(lines)
