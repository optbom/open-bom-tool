"""Human-friendly reference designator display.

Stored refdes lists use plain string order (part of the hashed form). For
display, sort naturally and collapse consecutive runs: DIMM0-DIMM31, R7.
"""

from __future__ import annotations

import re
from typing import Iterable

_SPLIT = re.compile(r"(.*?)([0-9]+)")


def _natural(ref: str) -> tuple[str, int, str]:
    m = _SPLIT.fullmatch(ref)
    return (m.group(1), int(m.group(2)), ref) if m else (ref, -1, ref)


def compact_refdes(refs: Iterable[str]) -> str:
    parts: list[str] = []
    run: list[tuple[str, int, str]] = []

    def flush() -> None:
        if len(run) >= 3:
            parts.append(f"{run[0][2]}-{run[-1][2]}")
        else:
            parts.extend(r[2] for r in run)
        run.clear()

    for item in sorted({_natural(r) for r in refs}):
        prefix, num, _ = item
        if run and (num < 0 or prefix != run[-1][0] or num != run[-1][1] + 1):
            flush()
        if num < 0:
            parts.append(item[2])
        else:
            run.append(item)
    flush()
    return ",".join(parts)


def refdes_change(old: Iterable[str], new: Iterable[str]) -> str:
    old, new = set(old), set(new)
    added, removed = compact_refdes(new - old), compact_refdes(old - new)
    return " ".join(x for x in (f"+{added}" if added else "", f"-{removed}" if removed else "") if x)
