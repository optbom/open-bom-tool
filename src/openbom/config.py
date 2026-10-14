"""Home directory resolution and CLI state (acting org, user, current workspace)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME

HOME_ENV = f"{APP_NAME.upper()}_HOME"
DEFAULT_HOME = Path("~") / f".{APP_NAME}"
DB_FILENAME = f"{APP_NAME}.db"
CONFIG_FILENAME = "config.json"


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    """--home wins, then $OPENBOM_HOME, then ~/.openbom."""
    if home is None:
        home = os.environ.get(HOME_ENV) or DEFAULT_HOME
    return Path(home).expanduser()


def db_path(home: Path) -> Path:
    return home / DB_FILENAME


# Identity is configured once, like `git config user.name`, and can be overridden per
# process, like GIT_AUTHOR_NAME. (D11: real authentication will replace this.)
ORG_ENV = f"{APP_NAME.upper()}_ORG"
USER_ENV = f"{APP_NAME.upper()}_USER"


@dataclass
class Config:
    org: str | None = None  # the org this installation acts for
    user: str | None = None  # display name written into commit authors
    # Current workspace, kept per org so a shared database used by several
    # (simulated) orgs doesn't point one org at another's workspace.
    workspaces: dict[str, str] = field(default_factory=dict)

    @property
    def effective_org(self) -> str | None:
        return os.environ.get(ORG_ENV) or self.org

    @property
    def effective_user(self) -> str | None:
        if os.environ.get(ORG_ENV):  # a different identity: don't borrow the configured user's name
            return os.environ.get(USER_ENV) or None
        return os.environ.get(USER_ENV) or self.user

    @property
    def identity_source(self) -> str:
        return f"${ORG_ENV}" if os.environ.get(ORG_ENV) else "config"

    @property
    def current_workspace(self) -> str | None:
        org = self.effective_org
        return None if org is None else self.workspaces.get(org)

    def use_workspace(self, ws_id: str) -> None:
        if self.effective_org is not None:
            self.workspaces[self.effective_org] = ws_id


def load_config(home: Path) -> Config:
    path = home / CONFIG_FILENAME
    if not path.exists():
        return Config()
    data = json.loads(path.read_text())
    return Config(
        org=data.get("org") or data.get("acting_org"),  # "acting_org": earlier prototype key
        user=data.get("user"),
        workspaces=dict(data.get("workspaces", {})),
    )


def save_config(home: Path, config: Config) -> None:
    home.mkdir(parents=True, exist_ok=True)
    data = {"org": config.org, "user": config.user, "workspaces": config.workspaces}
    (home / CONFIG_FILENAME).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
