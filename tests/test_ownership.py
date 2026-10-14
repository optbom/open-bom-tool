"""§11 Ownership (test 16) and specific error messages."""

from conftest import build_cross_org

from openbom.config import ORG_ENV, USER_ENV


# 16
def test_cannot_change_another_orgs_things(cli):
    build_cross_org(cli)  # leaves acme as the acting org
    owner = "owned by org 'pwrco'; you are acting as 'acme'"
    assert owner in cli.fail("part", "revise", "pwrco:FAN-40", "--rev", "Z").output
    assert owner in cli.fail("edge", "add", "PSU-800", "FAN-40", "--qty", "2", "-w", "pwrco-psu-ws").output
    assert owner in cli.fail("edge", "rm", "PSU-800", "FAN-40", "-w", "pwrco-psu-ws").output
    assert owner in cli.fail("update", "--all", "-w", "pwrco-psu-ws").output
    assert owner in cli.fail("commit", "-m", "hijack", "-w", "pwrco-psu-ws").output
    assert owner in cli.fail("part", "add", "pwrco:NEW", "--rev", "A", "--name", "x").output
    # Referencing another org's part as an own child is refused with a hint.
    assert "add-external" in cli.fail("edge", "add", "SRV", "pwrco:FAN-40", "--qty", "1").output


def test_specific_errors(cli):
    build_cross_org(cli)
    assert "part acme:NOPE does not exist" in cli.fail("edge", "add", "SRV", "NOPE", "--qty", "1").output
    assert "invalid quantity" in cli.fail("edge", "add", "SRV", "SCR", "--qty", "0").output
    assert "already exists" in cli.fail("edge", "add", "SRV", "CHS", "--qty", "1").output
    assert "workspace 'nope' does not exist" in cli.fail("edge", "add-external", "SRV", "--from", "nope", "--qty", "1").output
    assert "already exists" in cli.fail("part", "add", "SRV", "--rev", "A", "--name", "dup").output
    assert "invalid identifier scheme" in cli.fail(
        "part", "add", "X1", "--rev", "A", "--name", "x", "--id", "upc=123"
    ).output


def test_any_org_can_read_other_orgs_workspaces(cli):
    build_cross_org(cli)
    cli.run("part", "show", "pwrco:PSU-800", "--history")
    cli.run("tree", "HEAD", "-w", "pwrco-psu-ws")
    cli.run("verify", "-w", "pwrco-psu-ws")


def test_identity_is_configured_once_and_env_overrides(cli):
    cli.run("org", "create", "acme")
    cli.run("org", "create", "pwrco")
    assert "no org configured" in cli.fail("part", "add", "X", "--rev", "A", "--name", "x").output
    cli.run("config", "--org", "acme", "--user", "alice")  # once, like `git config user.name`
    cli.run("part", "add", "X", "--rev", "A", "--name", "x")
    cli.run("ws", "create", "ws", "--root", "X")
    cli.run("commit", "-m", "first")
    assert "Author: acme (alice)" in cli.run("log").stdout
    assert "acme  (from config)" in cli.run("whoami").stdout

    cli.env = {ORG_ENV: "pwrco", USER_ENV: "pat"}  # another installation's identity
    assert "pwrco  (from $OPENBOM_ORG)" in cli.run("whoami").stdout
    assert "owned by org 'acme'" in cli.fail("commit", "-m", "x", "-w", "ws").output
    assert "org 'nope' does not exist" in cli.fail("config", "--org", "nope").output
