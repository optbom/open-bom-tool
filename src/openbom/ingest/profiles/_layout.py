"""Helpers for profiles with a known, fixed header layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from ...errors import OpenBomError
from ..tabular import HEADER_SCAN_ROWS, Table, clean


@dataclass
class Layout:
    header_row: int
    index: dict[str, int]  # field -> column index
    headers: list[str]

    def get(self, cells: list[str], f: str) -> str:
        i = self.index.get(f)
        return clean(cells[i]) if i is not None and i < len(cells) else ""

    def describe(self) -> dict[str, str]:
        return {f: self.headers[i] for f, i in self.index.items()}

    def ignored(self) -> list[str]:
        used = set(self.index.values())
        return [h for i, h in enumerate(self.headers) if h and i not in used]


def find_header(
    table: Table, fields: Mapping[str, str], normalize: Callable[[str], str], required: tuple[str, ...]
) -> Layout | None:
    """Find the first row whose (normalized) cells contain the required headers."""
    wanted = {name.lower(): f for f, name in fields.items()}
    for row_no, cells in table.rows[:HEADER_SCAN_ROWS]:
        index: dict[str, int] = {}
        for i, cell in enumerate(cells):
            f = wanted.get(normalize(cell).lower())
            if f is not None and f not in index:
                index[f] = i
        if all(f in index for f in required):
            return Layout(row_no, index, [normalize(c) for c in cells])
    return None


def require_header(table: Table, profile: str, *args) -> Layout:
    layout = find_header(table, *args)
    if layout is None:
        raise OpenBomError(f"{table.path}: no {profile} header row found in the first {HEADER_SCAN_ROWS} rows")
    return layout
