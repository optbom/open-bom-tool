"""Oracle Fusion Cloud PLM "BOM Explosion With MPN/SPN" report export.

Layout notes (from real exports):
- Title rows above the header; the header has "Top Item Number" and " BOM Level".
- One row per approved manufacturer/vendor entry: a BOM line repeats once per
  MPN (Part Type "Manufacturer Part") or vendor part ("Vendor Part"), so
  consecutive rows with the same level, item, and SEQ are one line.
- Vendor parts are the supplier's own part numbers for the item: cross-references.
- Levels are "1", ".2", "..3".
"""

from __future__ import annotations

from typing import Mapping

from ..source import SourceBOM, fold_substitutes
from ..tabular import Row, Table, normalize_quantity, normalize_uom, parse_level, parse_refdes
from ._layout import find_header, require_header

NAME = "fusion"

FIELDS = {
    "top_item": "Top Item Number",
    "level": "BOM Level",
    "part_number": "Item Number",
    "description": "Description",
    "revision": "Rev",
    "seq": "SEQ",
    "quantity": "QTY",
    "uom": "UOM",
    "refdes": "Ref Des",
    "is_substitute": "BOM.Is Substitute?",
    "primary": "BOM.Primary Component",
    "mpn": "MPN",
    "manufacturer": "Manufacturer",
    "part_type": "Part Type",
    "spn": "SPN",
    "mfr_ref": "MFR Part Reference",
}
REQUIRED = ("top_item", "level", "part_number", "quantity")


def _normalize(header: str) -> str:
    return " ".join(header.split())


def detect(table: Table) -> bool:
    return find_header(table, FIELDS, _normalize, REQUIRED) is not None


def parse(table: Table, columns: Mapping[str, str] | None = None) -> SourceBOM:
    layout = require_header(table, NAME, FIELDS, _normalize, REQUIRED)
    bom = SourceBOM(
        profile=NAME,
        file=str(table.path),
        sheet=table.sheet,
        header_row=layout.header_row,
        columns=layout.describe(),
        ignored_columns=layout.ignored(),
        rows=[],
    )
    entries: list[tuple[Row, str | None, int | None]] = []
    approved: dict[int, list[tuple[str, str]]] = {}
    suppliers: dict[int, list[str]] = {}
    references: dict[int, list[str]] = {}
    last_key = None
    for line, cells in table.rows:
        if line <= layout.header_row or not any(c.strip() for c in cells):
            continue
        g = lambda f: layout.get(cells, f)  # noqa: E731
        pn, raw_level = g("part_number"), g("level")
        if not pn:
            continue
        key = (raw_level, pn, g("seq"))
        if key != last_key:  # a new BOM line
            last_key = key
            raw_qty = g("quantity")
            row = Row(
                line=line,
                level=parse_level(raw_level),
                pn=pn,
                raw_level=raw_level,
                raw_quantity=raw_qty,
                quantity=normalize_quantity(raw_qty) if raw_qty else None,
                revision=g("revision") or None,
                name=g("description") or None,
                uom=normalize_uom(g("uom")),
                refdes=tuple(parse_refdes(g("refdes"))),
            )
            is_sub = g("is_substitute").lower() in ("yes", "y", "true")
            entries.append((row, g("primary") if is_sub else None, None))
        row = entries[-1][0]
        mpn, mfr, part_type = g("mpn"), g("manufacturer"), g("part_type").lower()
        if mpn and part_type == "vendor part":
            suppliers.setdefault(id(row), []).append(mpn)
        elif mpn:
            approved.setdefault(id(row), []).append((mfr or "?", mpn))
        if g("spn"):
            suppliers.setdefault(id(row), []).append(g("spn"))
        if g("mfr_ref"):  # the MPN a vendor part stands for: approved by reference
            references.setdefault(id(row), []).append(g("mfr_ref"))
    for row, _, _ in entries:
        amls = list(dict.fromkeys(approved.get(id(row), [])))
        if amls:
            row.manufacturer, row.mpn = amls[0]
            if len(amls) > 1:
                row.aml = tuple(amls)
        listed = {m for _, m in amls}
        row.identifiers = tuple(("supplier", v) for v in dict.fromkeys(suppliers.get(id(row), []))) + tuple(
            ("mpn", v) for v in dict.fromkeys(references.get(id(row), [])) if v not in listed
        )
    bom.rows = fold_substitutes(entries, bom)
    return bom
