"""`status`: the draft compared with HEAD (changed edges, revised parts,
outdated externals). Composes workspace, diff, and sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .diff import DiffResult, diff_configurations
from .errors import OpenBomError
from .graph import overlay
from .store import Store, Workspace
from .sync import UP_TO_DATE, ExternalStatus, draft_externals
from .workspace import build_draft, get_workspace, head


@dataclass
class Status:
    workspace: Workspace
    head: str | None
    head_configuration: str | None
    draft_configuration: str | None
    diff: DiffResult | None
    externals: list[ExternalStatus] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    build_error: str | None = None

    @property
    def clean(self) -> bool:
        return self.build_error is None and self.head_configuration == self.draft_configuration

    @property
    def outdated(self) -> list[ExternalStatus]:
        return [e for e in self.externals if e.status != UP_TO_DATE]


def status(store: Store, ws_id: str) -> Status:
    ws = get_workspace(store, ws_id)
    current = head(store, ws.id)
    head_hash, head_comp = (current[0], current[1].configuration) if current else (None, None)
    result = Status(
        workspace=ws,
        head=head_hash,
        head_configuration=head_comp,
        draft_configuration=None,
        diff=None,
        externals=draft_externals(store, ws.id),
    )
    try:
        build = build_draft(store, ws)
    except OpenBomError as exc:
        result.build_error = str(exc)
        return result
    result.draft_configuration = build.root
    result.warnings = build.warnings
    get = overlay(build.objects, store.get_object)
    result.diff = diff_configurations(get, head_comp, build.root)
    return result
