"""Oracle Agile PLM BOM export (web client "Export" of an item's BOM with manufacturers).

Layout notes (from real exports):
- Headers may be bilingual ("Number/Số"); the part before "/" is the field name.
  Tab prefixes like "BOM." and "Manufacturers." name the Agile table a column comes from.
- One row per BOM line. Substitutes are separate rows with BOM.Alternative = ALT,
  pointing at their primary via BOM.Primary Number; an ALT row may come before its PRI.
- Rev holds the revision and the ECO number in one cell ("005        ECSYNZ17323005").
- BOM.Customer Part No is the customer's part number for the item ("TBD" when unknown).
- Additional approved manufacturer parts appear as rows with no Level/Number.
"""

from __future__ import annotations

from typing import Mapping

from ..source import SourceBOM, fold_substitutes
from ..tabular import Row, Table, normalize_quantity, normalize_uom, parse_level, parse_refdes, split_values
from ._layout import find_header, require_header

NAME = "agile"

FIELDS = {
    "level": "Level",
    "part_number": "Number",
    "description": "Description",
    "revision": "Rev",
    "quantity": "BOM.Qty",
    "uom": "BOM.Unit(BOM)",
    "uom_part": "BOM.Unit(Part)",
    "alternative": "BOM.Alternative",
    "priority": "BOM.Priority",
    "refdes": "BOM.Ref Des",
    "primary": "BOM.Primary Number",
    "customer_pn": "BOM.Customer Part No",
    "manufacturer": "Manufacturers.Mfr Name",
    "mpn": "Manufacturers.Mfr Part Number",
}
REQUIRED = ("level", "part_number", "quantity")


def _normalize(header: str) -> str:
    return header.split("/")[0].strip()


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
    aml: dict[int, list[tuple[str, str]]] = {}
    for line, cells in table.rows:
        if line <= layout.header_row or not any(c.strip() for c in cells):
            continue
        g = lambda f: layout.get(cells, f)  # noqa: E731
        pn, raw_level = g("part_number"), g("level")
        mfr, mpn = g("manufacturer"), g("mpn")
        if not pn and not raw_level:
            if entries and mpn:  # another approved manufacturer part for the row above
                aml.setdefault(id(entries[-1][0]), []).append((mfr or "?", mpn))
            continue
        raw_qty = g("quantity")
        rev_tokens = g("revision").split()
        row = Row(
            line=line,
            level=parse_level(raw_level),
            pn=pn,
            raw_level=raw_level,
            raw_quantity=raw_qty,
            quantity=normalize_quantity(raw_qty) if raw_qty else None,
            revision=rev_tokens[0] if rev_tokens else None,
            name=g("description") or None,
            uom=normalize_uom(g("uom") or g("uom_part")),
            refdes=tuple(parse_refdes(g("refdes"))),
            manufacturer=mfr or None,
            mpn=mpn or None,
            identifiers=tuple(("customer", v) for v in split_values(g("customer_pn"))),
        )
        is_alt = g("alternative").upper() == "ALT"
        priority = g("priority")
        entries.append((row, g("primary") if is_alt else None, int(priority) if priority.isdigit() else None))
        if mfr and mpn:
            aml.setdefault(id(row), []).append((mfr, mpn))
    for row, _, _ in entries:
        approved = aml.get(id(row), [])
        if len(approved) > 1:
            row.aml = tuple(approved)
    bom.rows = fold_substitutes(entries, bom)
    return bom
