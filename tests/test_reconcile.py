"""PLM profiles (Agile, Fusion), substitutes and AML in the model, and reconciliation."""

import json

import pytest
from plm_fixtures import agile_file, agile_rev2_rows, fusion_file

from openbom.graph import require
from openbom.ingest.profiles import load_source
from openbom.objects import Configuration, Node, encode, make_entry, make_node, parse_object
from openbom.workspace import head


@pytest.fixture
def files(tmp_path):
    return {"agile": agile_file(tmp_path / "agile.xlsx"), "fusion": fusion_file(tmp_path / "fusion.xlsx")}


def setup_two_sides(cli, files):
    cli.run("org", "create", "customer")
    cli.run("org", "create", "odm")
    cli.act("customer", "dana")
    cli.run("import", files["fusion"], "--new-ws", "cust-tray", "--apply")
    cli.run("commit", "-m", "Fusion EBOM")
    cli.act("odm", "lin")
    cli.run("import", files["agile"], "--new-ws", "odm-tray", "--apply")
    cli.run("commit", "-m", "Agile MBOM")
    cli.act("customer", "dana")
    cli.run("reconcile", "init", "recon", "cust-tray", "odm-tray")


def status(cli):
    return json.loads(cli.run("reconcile", "status", "--json").stdout)


def kinds(plan):
    return {(f["kind"], tuple(f["left"]), tuple(f["right"])) for f in plan["findings"] if f["kind"] != "match"}


# -- model ------------------------------------------------------------------------


def test_aml_and_substitutes_round_trip_and_normalize():
    node = make_node(org="a", part_id="P", revision="A", name="p", aml=[("Z", "2"), ("A", "1"), ("A", "1")])
    assert node.aml == (("A", "1"), ("Z", "2"))
    assert parse_object(node.body()) == node
    n1, n2, n3 = ("sha256:" + c * 64 for c in "abc")
    e = make_entry(node=n1, quantity="2", substitutes=[(n3, None), (n2, 2)])
    assert e.body()["substitutes"] == [{"node": n2, "priority": 2}, {"node": n3}]
    with pytest.raises(Exception, match="own substitute"):
        make_entry(node=n1, quantity="1", substitutes=[(n1, 1)])


def test_new_optional_fields_leave_existing_hashes_alone():
    node = make_node(org="acme", part_id="X", revision="A", name="x")
    assert "aml" not in node.body() and encode(node)[0] == encode(make_node(org="acme", part_id="X", revision="A", name="x"))[0]


# -- profiles -----------------------------------------------------------------------


def test_agile_profile(files):
    src = load_source(files["agile"])
    assert src.profile == "agile" and src.issues == []
    by_pn = {r.pn: r for r in src.rows}
    assert by_pn["MT-TRAY"].revision == "005"  # ECO number split off
    assert by_pn["MT-BOARD"].identifiers == (("customer", "C-BOARD"),)
    assert by_pn["MT-BAYB"].identifiers == ()  # "TBD" is a placeholder
    fan = by_pn["MT-FAN-P"]
    assert [(p, s.pn) for p, s in fan.substitutes] == [(2, "MT-FAN-A")]  # ALT came before its PRI
    assert "MT-FAN-A" not in by_pn


def test_fusion_profile_groups_manufacturer_rows(files):
    src = load_source(files["fusion"])
    assert src.profile == "fusion" and src.header_row == 3
    assert [r.pn for r in src.rows] == ["C-TRAY", "C-BOARD", "C-SSD", "C-BAYA", "C-BAYC", "C-BOOT", "C-FAN", "C-COOLANT"]
    ssd = src.rows[2]
    assert ssd.quantity == "4" and ssd.aml == (("DRIVECO", "MPN-SSD-A"), ("OTHERCO", "MPN-SSD-B"))
    assert (ssd.manufacturer, ssd.mpn) == ("DRIVECO", "MPN-SSD-A")
    assert src.rows[0].identifiers == (("supplier", "ODM-TRAY"),)


def test_csv_examples_still_use_generic_profile():
    from pathlib import Path

    assert load_source(Path(__file__).parent.parent / "examples" / "psu-bom.csv").profile == "generic"


def test_import_stores_substitutes_and_aml(cli, files):
    cli.run("org", "create", "odm")
    cli.act("odm")
    cli.run("import", files["agile"], "--new-ws", "odm-tray", "--apply")
    cli.run("commit", "-m", "x")
    out = cli.run("tree").stdout
    assert "or substitute: odm:MT-FAN-A rev 001" in out
    store = cli.store()
    _, commit = head(store, "odm-tray")
    root = require(store.get_object, commit.configuration, Configuration)
    fan = next(e for e in root.entries if require(store.get_object, e.node, Node).part_id == "MT-FAN-P")
    assert len(fan.substitutes) == 1 and fan.substitutes[0][1] == 2
    cli.run("verify")


# -- reconciliation -------------------------------------------------------------------


def test_reconcile_finds_pairs_and_classifies(cli, files):
    setup_two_sides(cli, files)
    plan = status(cli)
    pairs = {(p["left"], p["right"]) for p in plan["pairs"]}
    assert ("customer:C-TRAY", "odm:MT-TRAY") in pairs  # via the customer's vendor part number
    assert ("customer:C-BOARD", "odm:MT-BOARD") in pairs  # via the ODM's customer part number
    fan = next(p for p in plan["pairs"] if p["left"] == "customer:C-FAN")
    assert fan["right"] == "odm:MT-FAN-P" and fan["substitutes"] == ["odm:MT-FAN-A"]  # folded, not double counted
    assert [(p["left"], p["reason"]) for p in plan["proposals"]] == [("customer:C-BOOT", "weak")]

    found = kinds(plan)
    assert ("regrouped", ("customer:C-SSD",), ("odm:MT-SSD",)) in found  # 4 = 4, via the ODM's Bay B module
    assert ("moved", ("customer:C-BAYA",), ("odm:MT-BAYA",)) in found  # tray vs inside Bay C
    assert ("identity_conflict", ("customer:C-BOOT",), ("odm:MT-SSD",)) in found
    assert ("left_only", ("customer:C-COOLANT",), ()) in found
    assert ("right_only", (), ("odm:MT-SCREW",)) in found
    assert not any(k == "sourcing" for k, *_ in found)


def test_decisions_resolve_proposals_and_commit(cli, files):
    setup_two_sides(cli, files)
    cli.run("reconcile", "pair", "customer:C-BOOT", "odm:MT-SSD")
    cli.run("reconcile", "accept", "customer:C-COOLANT", "--note", "customer-consigned")
    plan = status(cli)
    assert plan["proposals"] == []
    found = kinds(plan)
    assert ("regrouped", ("customer:C-BOOT", "customer:C-SSD"), ("odm:MT-SSD",)) in found  # one ODM part, two customer PNs
    assert not any(k in ("identity_conflict", "quantity_mismatch") for k, *_ in found)
    coolant = next(f for f in plan["findings"] if f["kind"] == "left_only")
    assert coolant["accepted"] == "customer-consigned"

    assert "use `reconcile commit`" in cli.fail("commit", "-m", "x").output
    out = cli.run("reconcile", "commit", "-m", "DVT reconciliation").stdout
    store = cli.store()
    _, commit = head(store, "recon")
    assert commit.correspondence is not None and commit.correspondence in out
    assert plan["correspondence"] == commit.correspondence  # status previews the exact hash
    assert "OK" in cli.run("verify", "--reveal").stdout
    assert "nothing to commit" in cli.fail("reconcile", "commit", "-m", "again").output


def test_supplier_change_shows_up_as_reconciliation_drift(cli, files, tmp_path):
    setup_two_sides(cli, files)
    cli.run("reconcile", "pair", "customer:C-BOOT", "odm:MT-SSD")
    cli.run("reconcile", "commit", "-m", "v1")

    cli.act("odm")
    cli.run("import", agile_file(tmp_path / "agile2.xlsx", agile_rev2_rows()), "-w", "odm-tray", "--apply")
    cli.run("commit", "-m", "ECO")
    cli.act("customer")
    assert "odm-tray" in cli.run("outdated", "-w", "recon").stdout
    cli.run("update", "--all", "-w", "recon")
    plan = status(cli)
    new = {(f["kind"], tuple(f["left"])) for f in plan["new_findings"]}
    assert new == {("quantity_mismatch", ("customer:C-BOARD",)), ("sourcing", ("customer:C-FAN",))}
    assert "MPN-FAN-9" in next(f["detail"] for f in plan["new_findings"] if f["kind"] == "sourcing")

    cli.run("reconcile", "commit", "-m", "v2")
    out = cli.run("reconcile", "diff").stdout
    assert "+ quantity" in out and "+ sourcing" in out
    assert "- match" not in out


def test_only_the_runner_can_change_a_reconciliation(cli, files):
    setup_two_sides(cli, files)
    cli.act("odm")
    owner = "owned by org 'customer'"
    assert owner in cli.fail("reconcile", "pair", "customer:C-BOOT", "odm:MT-SSD", "-w", "recon").output
    assert owner in cli.fail("reconcile", "commit", "-m", "x", "-w", "recon").output
    cli.run("reconcile", "status", "-w", "recon")  # but anyone can read it


def test_prefix_stripping_adds_supplier_evidence(cli, files):
    setup_two_sides(cli, files)
    cli.run("reconcile", "init", "recon2", "cust-tray", "odm-tray", "--strip-prefix", "odm-tray=MT-")
    def board_evidence(ws):
        plan = json.loads(cli.run("reconcile", "status", "--json", "-w", ws).stdout)
        return next(p["evidence"] for p in plan["pairs"] if p["left"] == "customer:C-BOARD")

    assert "supplier_pn" not in board_evidence("recon")
    assert "supplier_pn" in board_evidence("recon2")


def test_sides_are_symmetric_for_decisions(cli, files):
    setup_two_sides(cli, files)
    out = cli.run("reconcile", "pair", "odm:MT-SSD", "customer:C-BOOT").stdout  # either order
    assert "customer:C-BOOT <-> odm:MT-SSD" in out
    assert "on the same side" in cli.fail("reconcile", "pair", "customer:C-BOOT", "customer:C-SSD").output
    assert "neither cust-tray nor odm-tray" in cli.fail("reconcile", "accept", "NOPE").output
    cli.run("reconcile", "accept", "MT-SCREW", "--note", "ODM detail")  # bare part IDs resolve per side
    assert "only in" in cli.run("reconcile", "status").stdout and "left" not in cli.run("reconcile", "status").stdout


def test_approver_defaults_to_the_runners_side(cli, files, tmp_path):
    setup_two_sides(cli, files)
    cli.act("odm")
    cli.run("import", agile_file(tmp_path / "agile2.xlsx", agile_rev2_rows()), "-w", "odm-tray", "--apply")
    cli.run("commit", "-m", "ECO")
    cli.run("org", "create", "auditor")
    cli.act("auditor")
    out = cli.run("reconcile", "init", "audit", "cust-tray", "odm-tray").stdout
    assert "no sourcing check" in out  # the auditor owns neither side
    assert not any(f["kind"] == "sourcing" for f in status(cli)["findings"])
    cli.run("reconcile", "init", "audit2", "cust-tray", "odm-tray", "--approver", "cust-tray")
    found = json.loads(cli.run("reconcile", "status", "--json", "-w", "audit2").stdout)["findings"]
    sourcing = [f["detail"] for f in found if f["kind"] == "sourcing"]
    assert sourcing == ["odm-tray uses MPN-FAN-9, not on cust-tray's approved list"]
