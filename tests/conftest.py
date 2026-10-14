from pathlib import Path

import pytest
from typer.testing import CliRunner

from openbom.cli import app
from openbom.config import ORG_ENV, USER_ENV
from openbom.graph import require
from openbom.objects import Configuration, Entry, Node
from openbom.store import Store
from openbom.workspace import head


class Cli:
    """Runs CLI commands against a temporary home (passed as a per-command --home)."""

    def __init__(self, home: Path):
        self.home = home
        self.runner = CliRunner()
        self.env: dict[str, str | None] = {ORG_ENV: None, USER_ENV: None}

    def act(self, org: str, user: str | None = None) -> None:
        """Run later commands from this org's installation (its identity is ambient, like git's)."""
        self.env = {ORG_ENV: org, USER_ENV: user}

    def run(self, *args: str, ok: bool = True):
        result = self.runner.invoke(app, [*args, "--home", str(self.home)], env=self.env)
        if ok:
            assert result.exit_code == 0, f"{args} failed:\n{result.output}\n{result.exception!r}"
        return result

    def fail(self, *args: str):
        result = self.run(*args, ok=False)
        assert result.exit_code != 0, f"{args} unexpectedly succeeded:\n{result.output}"
        assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
        return result

    def store(self) -> Store:
        return Store.open(self.home)


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    c = Cli(tmp_path / "home")
    c.run("init")
    return c


# -- graph inspection helpers ----------------------------------------------


def root_configuration(store: Store, ws_id: str) -> tuple[str, Configuration]:
    _, commit = head(store, ws_id)
    return commit.configuration, require(store.get_object, commit.configuration, Configuration)


def entry_for(store: Store, comp: Configuration, part_key: str) -> Entry:
    for e in comp.entries:
        if require(store.get_object, e.node, Node).key == part_key:
            return e
    raise AssertionError(f"no entry for {part_key}")


def child_configuration(store: Store, comp: Configuration, part_key: str) -> tuple[str, Configuration]:
    e = entry_for(store, comp, part_key)
    return e.configuration, require(store.get_object, e.configuration, Configuration)


# -- scenarios ---------------------------------------------------------------


def build_acme_server(cli: Cli) -> None:
    """acme: SRV -> {CHS -> SCR x8, MB -> {CPU x2, DIMM x8}}, committed."""
    cli.run("org", "create", "acme", "--name", "Acme Compute")
    cli.act("acme", "alice")
    cli.run("part", "add", "SRV", "--rev", "A", "--name", "1U compute server")
    cli.run("part", "add", "CHS", "--rev", "A", "--name", "1U chassis")
    cli.run(
        "part", "add", "SCR", "--rev", "A", "--name", "M3x6 screw",
        "--manufacturer", "Acme Fasteners", "--mpn", "M3-6-SS",
    )
    cli.run("part", "add", "MB", "--rev", "A", "--name", "Motherboard")
    cli.run("part", "add", "CPU", "--rev", "A", "--name", "CPU", "--mpn", "X1")
    cli.run("part", "add", "DIMM", "--rev", "A", "--name", "32GB DIMM")
    cli.run("ws", "create", "acme-server-ws", "--root", "SRV")
    cli.run("edge", "add", "SRV", "CHS", "--qty", "1")
    cli.run("edge", "add", "CHS", "SCR", "--qty", "8")
    cli.run("edge", "add", "SRV", "MB", "--qty", "1")
    cli.run("edge", "add", "MB", "CPU", "--qty", "2", "--refdes", "CPU0", "--refdes", "CPU1")
    cli.run("edge", "add", "MB", "DIMM", "--qty", "8")
    cli.run("commit", "-m", "initial server")


def build_pwrco_psu(cli: Cli) -> None:
    """pwrco: PSU -> {FAN, PCB}, committed."""
    cli.run("org", "create", "pwrco", "--name", "Power Co")
    cli.act("pwrco", "pat")
    cli.run("part", "add", "PSU-800", "--rev", "C", "--name", "800W PSU")
    cli.run("part", "add", "FAN-40", "--rev", "A", "--name", "40mm fan")
    cli.run("part", "add", "PCB-1", "--rev", "A", "--name", "PSU board")
    cli.run("ws", "create", "pwrco-psu-ws", "--root", "PSU-800")
    cli.run("edge", "add", "PSU-800", "FAN-40", "--qty", "1")
    cli.run("edge", "add", "PSU-800", "PCB-1", "--qty", "1")
    cli.run("commit", "-m", "PSU initial")


def build_cross_org(cli: Cli) -> None:
    """pwrco publishes a PSU; acme's server references it as an external entry."""
    build_pwrco_psu(cli)
    build_acme_server(cli)
    cli.run("edge", "add-external", "SRV", "--from", "pwrco-psu-ws", "--qty", "2", "--refdes", "PSU1", "--refdes", "PSU2")
    cli.run("commit", "-m", "add PSU")
