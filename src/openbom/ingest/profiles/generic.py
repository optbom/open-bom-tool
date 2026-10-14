"""Generic profile: a plain indented BOM, one row per line, columns found by header synonyms."""

from __future__ import annotations

from typing import Mapping

from ..source import SourceBOM
from ..tabular import Table, detect_columns, read_rows

NAME = "generic"


def detect(table: Table) -> bool:
    return True


def parse(table: Table, columns: Mapping[str, str] | None = None) -> SourceBOM:
    cmap = detect_columns(table, columns)
    return SourceBOM(
        profile=NAME,
        file=str(table.path),
        sheet=table.sheet,
        header_row=cmap.header_row,
        columns=cmap.describe(),
        ignored_columns=cmap.ignored,
        rows=read_rows(table, cmap),
    )
