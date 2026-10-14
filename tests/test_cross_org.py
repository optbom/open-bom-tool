"""§11 Cross-org (tests 10-15)."""

import json

from conftest import build_cross_org, build_pwrco_psu, entry_for, root_configuration

from openbom.graph import require
from openbom.objects import Configuration, encode, make_configuration
from openbom.verify import verify_commit
from openbom.workspace import head

ACME_WS = "acme-server-ws"
PSU_WS = "pwrco-psu-ws"


# 10
def test_other_orgs_workspaces_are_listed(cli):
    build_pwrco_psu(cli)
    cli.run("org", "create", "acme")
    cli.act("acme")
    out = cli.run("ws", "list").stdout
    assert "pwrco-psu-ws  owner pwrco  root pwrco:PSU-800" in out


# 11
def test_external_entry_is_opaque(cli):
    build_cross_org(cli)
    out = cli.run("tree").stdout
    assert '2 ea  pwrco:PSU-800 rev C  "800W PSU"  (refdes: PSU1,PSU2)  ⟵ pwrco-psu-ws' in out
    assert "[opaque]" in out
    assert "FAN-40" not in out and "PCB-1" not in out

    tree = json.loads(cli.run("tree", "--json").stdout)
    psu = next(c for c in tree["children"] if c["part"] == "pwrco:PSU-800")
    assert psu["status"] == "opaque" and psu["children"] == [] and psu["source"] == PSU_WS


# 12
def test_supplier_change_is_pinned_then_updated(cli):
    build_cross_org(cli)
    store = cli.store()
    acme_head_before = head(store, ACME_WS)[0]
    root_before, comp_before = root_configuration(store, ACME_WS)
    psu_pinned = entry_for(store, comp_before, "pwrco:PSU-800").configuration

    cli.act("pwrco")
    cli.run("part", "revise", "FAN-40", "--rev", "B")
    cli.run("commit", "-m", "fan rev B")
    psu_latest = head(store, PSU_WS)[1].configuration
    assert psu_latest != psu_pinned

    cli.act("acme")
    assert head(store, ACME_WS)[0] == acme_head_before  # supplier commit did not touch acme
    out = cli.run("outdated").stdout
    assert "pwrco:PSU-800 ⟵ pwrco-psu-ws" in out and "rev C" in out

    cli.run("update", "--all")
    cli.run("commit", "-m", "pick up new PSU")
    root_after, comp_after = root_configuration(store, ACME_WS)
    assert root_after != root_before
    assert entry_for(store, comp_after, "pwrco:PSU-800").configuration == psu_latest
    assert "all external entries are up to date" in cli.run("outdated").stdout

    d = json.loads(cli.run("diff", "--json").stdout)
    assert len(d["opaque_changes"]) == 1
    change = d["opaque_changes"][0]
    assert (change["child"], change["old_configuration"], change["new_configuration"]) == (
        "pwrco:PSU-800", psu_pinned, psu_latest,
    )
    assert d["revised_parts"] == []  # the fan is not visible without --reveal
    assert "sub-assembly changed (contents not visible)" in cli.run("diff").stdout

    revealed = json.loads(cli.run("diff", "--reveal", "--json").stdout)
    assert [p["part"] for p in revealed["revised_parts"]] == ["pwrco:FAN-40"]


def test_update_single_part_and_status(cli):
    build_cross_org(cli)
    cli.act("pwrco")
    cli.run("part", "revise", "PCB-1", "--rev", "B")
    cli.run("commit", "-m", "pcb rev B")
    cli.act("acme")
    assert "outdated externals" in cli.run("status").stdout
    assert "no external entry" in cli.fail("update", "pwrco:NOPE").output
    assert "updated pwrco:PSU-800" in cli.run("update", "pwrco:PSU-800").stdout
    assert "Opaque changes" in cli.run("status").stdout


# 13
def test_reveal_expands_and_verifies_internals(cli):
    build_cross_org(cli)
    out = cli.run("tree", "--reveal").stdout
    assert "FAN-40" in out and "PCB-1" in out and "[opaque]" not in out

    plain = cli.run("verify").stdout
    revealed = cli.run("verify", "--reveal").stdout
    assert "opaque configurations (not traversed): 1" in plain
    assert "opaque configurations (not traversed): 0" in revealed
    count = lambda s: int(s.split("verified ")[1].split()[0])  # noqa: E731
    assert count(revealed) == count(plain) + 3  # PSU configuration, fan node, PCB node


# 14
def test_verify_detects_corruption(cli):
    build_cross_org(cli)
    assert "OK" in cli.run("verify").stdout
    store = cli.store()
    _, root = root_configuration(store, ACME_WS)
    _, chs = (lambda e: (e.configuration, require(store.get_object, e.configuration, Configuration)))(
        entry_for(store, root, "acme:CHS")
    )
    screw = entry_for(store, chs, "acme:SCR").node
    good = store.get_object(screw)
    store.conn.execute("UPDATE objects SET body = ? WHERE hash = ?", (good.replace(b"M3x6", b"M4x6"), screw))

    result = cli.fail("verify")
    assert "content hash mismatch" in result.output
    assert screw in result.output


def test_verify_only_reaches_supplier_internals_with_reveal(cli):
    build_cross_org(cli)
    store = cli.store()
    fan = store.get_part_node("pwrco", "FAN-40")
    store.conn.execute("UPDATE objects SET body = ? WHERE hash = ?", (b'{"tampered":true}', fan))
    cli.run("verify")  # opaque: not traversed
    assert "content hash mismatch" in cli.fail("verify", "--reveal").output


def test_verify_against_a_bundle():
    """D16 seam: verify runs over any object source, not just the database."""
    from openbom.objects import make_commit, make_entry, make_node

    objects = {}

    def put(obj):
        h, data = encode(obj)
        objects[h] = data
        return h

    leaf = put(make_node(org="acme", part_id="L", revision="A", name="leaf"))
    root_node = put(make_node(org="acme", part_id="R", revision="A", name="root"))
    comp = put(make_configuration(node=root_node, entries=[make_entry(node=leaf, quantity="1")], secret=b"k" * 32))
    commit = put(make_commit(workspace="ws", configuration=comp, parents=[], author_org="acme", message="m"))
    assert verify_commit(objects.get, commit).ok
    del objects[leaf]
    report = verify_commit(objects.get, commit)
    assert not report.ok and "missing" in report.failures[0]


def test_missing_pinned_object_renders_without_crashing(cli):
    build_cross_org(cli)
    store = cli.store()
    _, root = root_configuration(store, ACME_WS)
    psu_comp = entry_for(store, root, "pwrco:PSU-800").configuration
    store.conn.execute("DELETE FROM objects WHERE hash = ?", (psu_comp,))
    assert "[missing]" in cli.run("tree", "--reveal").stdout


# 15
def test_salting(cli):
    build_cross_org(cli)
    store = cli.store()
    acme_ws, pwrco_ws = store.get_workspace(ACME_WS), store.get_workspace(PSU_WS)
    # Two workspaces in different orgs, byte-identical unsalted content:
    _, root = root_configuration(store, ACME_WS)
    in_acme = make_configuration(node=root.node, entries=root.entries, secret=acme_ws.secret)
    in_pwrco = make_configuration(node=root.node, entries=root.entries, secret=pwrco_ws.secret)
    assert in_acme.unsalted_body() == in_pwrco.unsalted_body()
    assert encode(in_acme)[0] != encode(in_pwrco)[0]
    assert encode(in_acme)[0] == head(store, ACME_WS)[1].configuration

    # Recommitting unchanged content in one workspace reproduces the same hash.
    original = head(store, ACME_WS)[1].configuration
    cli.run("edge", "set", "SRV", "MB", "--qty", "2")
    cli.run("commit", "-m", "two boards")
    assert head(store, ACME_WS)[1].configuration != original
    cli.run("edge", "set", "SRV", "MB", "--qty", "1")
    cli.run("commit", "-m", "back to one")
    assert head(store, ACME_WS)[1].configuration == original
