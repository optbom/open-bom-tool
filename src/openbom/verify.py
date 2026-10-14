"""Integrity verification (§8.5).

Written against an abstract object source (`get(hash) -> bytes | None`) and an
optional source-history lookup, so it can later run over an offline
disclosure bundle (D16) instead of the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .canonical import canonical_bytes, hash_bytes, parse_bytes, short_hash
from .errors import SchemaError
from .graph import ObjectSource
from .objects import Commit, Configuration, Correspondence, StoredObject, object_type, parse_object

# source workspace ID -> root configuration hashes of its commit history, or None if unknown.
SourceHistory = Callable[[str], "set[str] | None"]


@dataclass
class VerifyReport:
    commit: str
    objects_verified: int = 0
    opaque: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def verify_commit(
    get: ObjectSource,
    commit_hash: str,
    *,
    reveal: bool = False,
    source_history: SourceHistory | None = None,
) -> VerifyReport:
    """Verify every object reachable from a commit.

    TODO(spec): only the given commit's graph is verified, not its parent
    commits (run verify per revision to check history).
    """
    report = VerifyReport(commit=commit_hash)
    checked: dict[str, StoredObject | None] = {}
    opaque: set[str] = set()
    histories: dict[str, set[str] | None] = {}
    GRAY, BLACK = 1, 2
    color: dict[str, int] = {}

    def fail(msg: str) -> None:
        report.failures.append(msg)

    def check(h: str, expected: str) -> StoredObject | None:
        if h in checked:
            return checked[h]
        checked[h] = None
        data = get(h)
        if data is None:
            fail(f"{expected} {h}: missing")
            return None
        actual = hash_bytes(data)
        if actual != h:
            fail(f"{expected} {h}: content hash mismatch (stored bytes hash to {short_hash(actual)})")
            return None
        try:
            value = parse_bytes(data)
            if canonical_bytes(value) != data:
                raise SchemaError("stored bytes are not canonical JSON")
            obj = parse_object(value)
        except SchemaError as exc:
            fail(f"{expected} {h}: {exc}")
            return None
        if object_type(obj) != expected:
            fail(f"{h}: expected a {expected}, found a {object_type(obj)}")
            return None
        report.objects_verified += 1
        checked[h] = obj
        return obj

    def check_source(source: str, configuration: str | None) -> None:
        if source_history is None:
            return
        if source not in histories:
            histories[source] = source_history(source)
        history = histories[source]
        if history is None:
            fail(f"external source workspace '{source}' not found")
        elif configuration is None or configuration not in history:
            fail(f"pinned configuration {configuration} is not in the commit history of '{source}'")

    def visit(h: str, expected_node: str | None, is_ws_root: bool) -> None:
        comp = check(h, "configuration")
        if comp is None:
            return
        assert isinstance(comp, Configuration)
        if expected_node is not None and comp.node != expected_node:
            fail(f"configuration {h}: root node {comp.node} does not match the entry node {expected_node}")
        state = color.get(h)
        if state == GRAY:
            fail(f"configuration {h}: cycle detected")
            return
        if state == BLACK:
            return
        color[h] = GRAY
        if not comp.entries and not is_ws_root:
            fail(f"configuration {h}: empty configurations are only valid as a workspace root")
        check(comp.node, "node")
        for e in comp.entries:
            check(e.node, "node")
            for sub, _ in e.substitutes:
                check(sub, "node")
            if e.is_external:
                check_source(e.source, e.configuration)
                if not reveal:
                    if e.configuration is not None:
                        opaque.add(e.configuration)
                    continue
            if e.configuration is not None:
                visit(e.configuration, e.node, is_ws_root=e.is_external)
        color[h] = BLACK

    commit = check(commit_hash, "commit")
    if isinstance(commit, Commit):
        visit(commit.configuration, None, is_ws_root=True)
        if commit.correspondence is not None:
            _check_correspondence(commit, check, fail)
    report.opaque = len(opaque)
    return report


def _check_correspondence(commit: Commit, check: Callable, fail: Callable[[str], None]) -> None:
    """A reconciliation commit's correspondence must pin exactly what its configuration pins."""
    corr = check(commit.correspondence, "correspondence")
    if not isinstance(corr, Correspondence):
        return
    root = check(commit.configuration, "configuration")
    if isinstance(root, Configuration):
        pins = {(e.source, e.configuration) for e in root.entries if e.is_external}
        for side, ws, comp in (
            ("left", corr.left_source, corr.left_configuration),
            ("right", corr.right_source, corr.right_configuration),
        ):
            if (ws, comp) not in pins:
                fail(f"correspondence {side} side ({ws} @ {short_hash(comp)}) is not pinned by the commit")
    for pair in corr.pairs:
        check(pair.left, "node")
        check(pair.right, "node")
    for node, _ in corr.accepted:
        check(node, "node")
