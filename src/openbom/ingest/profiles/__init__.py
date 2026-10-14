"""Ingestion profiles: one per source layout.

Each profile module provides:
    NAME: str
    detect(table) -> bool      recognise its layout (file profiles only)
    parse(table, columns) -> SourceBOM

`generic` is the fallback for plain indented BOMs. A PLM API connector would be
another profile that builds a SourceBOM from API responses instead of a table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ...errors import OpenBomError
from ..source import SourceBOM
from ..tabular import read_table
from . import agile, fusion, generic

PROFILES = {m.NAME: m for m in (agile, fusion, generic)}
DETECT_ORDER = (agile, fusion)  # generic is the fallback


def load_source(
    path: str | Path,
    *,
    profile: str | None = None,
    sheet: str | None = None,
    columns: Mapping[str, str] | None = None,
) -> SourceBOM:
    table = read_table(Path(path), sheet)
    if profile is None:
        module = next((m for m in DETECT_ORDER if m.detect(table)), generic)
    elif profile in PROFILES:
        module = PROFILES[profile]
    else:
        raise OpenBomError(f"unknown profile {profile!r}; profiles: {', '.join(sorted(PROFILES))}")
    if columns and module is not generic:
        raise OpenBomError(f"--column only applies to the generic profile, not {module.NAME}")
    return module.parse(table, columns)
