"""The shared intermediate format every ingestion profile produces.

A profile (an xlsx/csv layout today, a PLM API connector later) turns its input
into a SourceBOM: BOM lines in order with levels, part facts, cross-reference
identifiers, and line-level substitutes already folded in. The planner only
ever sees SourceBOMs, so where the data came from is purely an ingestion choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .tabular import Row

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Issue:
    severity: str
    message: str
    line: int | None = None
    hint: str | None = None


IssueFn = Callable[..., None]


@dataclass
class SourceBOM:
    profile: str
    file: str
    sheet: str | None
    header_row: int
    columns: dict[str, str]  # canonical field -> source header
    ignored_columns: list[str]
    rows: list[Row]  # BOM lines in source order; substitutes are attached to their primary
    issues: list[Issue] = field(default_factory=list)

    def issue(self, severity: str, message: str, line: int | None = None, hint: str | None = None) -> None:
        self.issues.append(Issue(severity, message, line, hint))


def fold_substitutes(entries: list[tuple[Row, str | None, int | None]], bom: SourceBOM) -> list[Row]:
    """Attach substitute rows to their primary line.

    entries: (row, primary part number if this row is a substitute, priority).
    A substitute attaches to the primary with that part number under the same
    parent. It may appear before or after its primary (Agile exports do both).
    """
    out: list[Row] = []
    stack: list[Row] = []
    primaries: dict[tuple[int | None, str], Row] = {}
    pending: list[tuple[int | None, str, int | None, Row]] = []
    for row, primary_pn, priority in entries:
        if row.level is None:
            out.append(row)  # the planner reports the bad level
            continue
        while stack and stack[-1].level >= row.level:
            stack.pop()
        parent_line = stack[-1].line if stack else None
        if primary_pn:
            pending.append((parent_line, primary_pn, priority, row))
            continue
        primaries.setdefault((parent_line, row.pn), row)
        out.append(row)
        stack.append(row)
    for parent_line, primary_pn, priority, row in pending:
        primary = primaries.get((parent_line, primary_pn))
        if primary is None:
            bom.issue(
                ERROR,
                f"substitute {row.pn} names primary {primary_pn}, which is not listed under the same parent",
                row.line,
            )
            continue
        primary.substitutes.append((priority, row))
    return out
