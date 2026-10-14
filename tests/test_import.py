"""BOM import: parsing, planning, applying, mappings, and re-import."""

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import root_configuration

from openbom.errors import OpenBomError
from openbom.ingest.tabular import (
    Table,
    detect_columns,
    normalize_quantity,
    parse_level,
    parse_refdes,
)
from openbom.render.refdes import compact_refdes, refdes_change
from openbom.store import SCHEMA_VERSION, Store
from openbom.workspace import head

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PSU = str(EXAMPLES / "psu-bom.csv")
SERVER_A = str(EXAMPLES / "server-bom-revA.csv")
SERVER_B = str(EXAMPLES / "server-bom-revB.csv")


def write(tmp_path, text, name="bom.csv"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def plan_json(cli, *args, ok=True):
    result = cli.run("import", *args, "--json", ok=ok)
    return json.loads(result.stdout)


def setup_orgs(cli):
    cli.run("org", "create", "acme")
    cli.run("org", "create", "pwrco")


def import_psu(cli):
    cli.act("pwrco")
    cli.run("import", PSU, "--new-ws", "pwrco-psu-ws", "--apply")
    cli.run("commit", "-m", "PSU from export")


def import_server_a(cli):
    cli.act("acme")
    cli.run("map", "add", "TIM-01", "--ignore")
    cli.run("map", "add", "PWR-0800", "--from", "pwrco-psu-ws")
    cli.run("import", SERVER_A, "--new-ws", "acme-server-ws", "--apply")
    cli.run("commit", "-m", "EBOM rev A")


# -- parsing ------------------------------------------------------------------


@pytest.mark.parametrize("raw,level", [("0", 0), ("3", 3), (".1", 1), ("..2", 2), ("...", 3), ("x", None), ("", None)])
def test_parse_level(raw, level):
    assert parse_level(raw) == level


@pytest.mark.parametrize(
    "raw,qty", [("2", "2"), ("1.0", "1"), ("0.50", "0.5"), ("1,000", "1000"), ("1E1", "10"), ("A/R", None), ("0", None), ("-2", None)]
)
def test_normalize_quantity(raw, qty):
    assert normalize_quantity(raw) == qty


def test_parse_refdes_expands_ranges():
    assert parse_refdes("C1-C4, R7") == ["C1", "C2", "C3", "C4", "R7"]
    assert parse_refdes("Q1-3 Q9") == ["Q1", "Q2", "Q3", "Q9"]
    assert parse_refdes("J1;J2") == ["J1", "J2"]


def test_compact_refdes():
    assert compact_refdes(f"DIMM{i}" for i in range(32)) == "DIMM0-DIMM31"
    assert compact_refdes(["R10", "R2", "R1", "C3", "C4", "C5", "X"]) == "C3-C5,R1,R2,R10,X"
    assert refdes_change(["R1", "R2"], ["R2", "R3"]) == "+R3 -R1"


def test_header_detection_skips_title_rows():
    table = Table(
        path=Path("x.csv"),
        rows=[
            (1, ["Acme BOM export", ""]),
            (2, ["", ""]),
            (3, ["Lvl", "P/N", "Rev.", "Qty Per", "Mfr P/N", "Notes"]),
        ],
    )
    cols = detect_columns(table)
    assert cols.header_row == 3
    assert cols.describe() == {"level": "Lvl", "part_number": "P/N", "revision": "Rev.", "quantity": "Qty Per", "mpn": "Mfr P/N"}
    assert cols.ignored == ["Notes"]


def test_header_override_and_missing_columns():
    table = Table(path=Path("x.csv"), rows=[(1, ["Depth", "Part Number", "Count"])])
    with pytest.raises(OpenBomError, match="no level, quantity column"):
        detect_columns(table)
    cols = detect_columns(table, {"level": "Depth", "quantity": "Count"})
    assert cols.columns == {"level": 0, "part_number": 1, "quantity": 2}


# -- planning and applying ----------------------------------------------------


def test_dry_run_writes_nothing(cli):
    setup_orgs(cli)
    cli.act("pwrco")
    plan = plan_json(cli, PSU, "--new-ws", "pwrco-psu-ws")
    assert plan["ready"] and not plan["applied"]
    assert len(plan["parts"]) == 6 and len(plan["edges"]) == 5
    assert [p["part"] for p in plan["preview"]["added_parts"]][0] == "pwrco:CAP-470U"
    store = cli.store()
    assert store.list_parts() == [] and store.list_workspaces() == []


def test_import_new_workspace_then_commit(cli):
    setup_orgs(cli)
    import_psu(cli)
    out = cli.run("tree", "-w", "pwrco-psu-ws").stdout
    assert '4 ea  pwrco:CAP-470U rev A  "470uF capacitor"  (mfr: Nichicon, mpn: UVR1V471MPD, refdes: C1-C4)' in out
    assert "2 ea  pwrco:MOSFET-60V rev A" in out and "refdes: Q1,Q2" in out
    cli.run("verify")


def test_errors_block_apply_and_mappings_resolve_them(cli):
    setup_orgs(cli)
    import_psu(cli)
    cli.act("acme")
    result = cli.fail("import", SERVER_A, "--new-ws", "acme-server-ws", "--apply")
    assert "TIM-01 quantity 'A/R' is not a positive number" in result.output
    assert 'openbom map add "TIM-01" --ignore' in result.output
    assert cli.store().list_workspaces()[0].id == "pwrco-psu-ws"  # nothing written

    cli.run("map", "add", "TIM-01", "--ignore")
    cli.run("map", "add", "PWR-0800", "--from", "pwrco-psu-ws")
    assert "'PWR-0800' -> external pwrco-psu-ws" in cli.run("map", "list").stdout
    plan = plan_json(cli, SERVER_A, "--new-ws", "acme-server-ws", "--apply")
    assert plan["applied"]
    assert "PWR-0800" not in {p["part_id"] for p in plan["parts"]}  # external, not an own part
    assert any("1 row under PWR-0800 skipped" in i["message"] for i in plan["issues"])
    cli.run("commit", "-m", "EBOM rev A")

    out = cli.run("tree").stdout
    assert "pwrco:PSU-800 rev C" in out and "[opaque]" in out
    assert "TIM-01" not in out and "FAN-40" not in out
    assert out.count("(see above)") == 1  # bracket assembly shared by chassis and board


def test_reimport_produces_exact_diff(cli):
    setup_orgs(cli)
    import_psu(cli)
    import_server_a(cli)
    cli.run("import", SERVER_A, "--apply")  # same file again: same draft
    assert "nothing to commit" in cli.fail("commit", "-m", "same").output

    cli.run("import", SERVER_B, "--apply")
    cli.run("commit", "-m", "EBOM rev B")
    d = json.loads(cli.run("diff", "--json").stdout)
    assert [(p["part"], p["old_revision"], p["new_revision"]) for p in d["revised_parts"]] == [("acme:CPU-X", "A", "B")]
    changes = {(e["parent"], e["child"], e["change"], tuple(e["fields"])) for e in d["edge_changes"]}
    assert changes == {
        ("acme:MB-X1", "acme:BRKT-DRV", "removed", ()),
        ("acme:MB-X1", "acme:DIMM-32G", "changed", ("quantity", "refdes")),
    }
    assert d["added_parts"] == d["removed_parts"] == d["opaque_changes"] == []
    assert "refdes +DIMM16-DIMM31" in cli.run("diff").stdout


def test_reimport_keeps_external_pin(cli):
    setup_orgs(cli)
    import_psu(cli)
    import_server_a(cli)
    store = cli.store()
    pinned = head(store, "pwrco-psu-ws")[1].configuration

    cli.act("pwrco")
    cli.run("part", "revise", "FAN-40", "--rev", "B")
    cli.run("commit", "-m", "fan B", "-w", "pwrco-psu-ws")
    cli.act("acme")
    plan = plan_json(cli, SERVER_A)
    ext = [e for e in plan["edges"] if e["external"]]
    assert ext[0]["pin_configuration"] == pinned and ext[0]["pin_kept"]
    assert plan["preview"]["opaque_changes"] == []

    cli.run("update", "--all")
    cli.run("commit", "-m", "new PSU")
    assert "Opaque changes" in cli.run("diff").stdout


def test_conflicting_part_facts(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    path = write(
        tmp_path,
        "Level,Part Number,Rev,Description,Qty\n0,TOP,A,Top,\n1,SUB,A,Sub,1\n2,SCR,A,Screw,4\n1,SCR,B,Screw,2\n",
    )
    out = cli.fail("import", path, "--new-ws", "ws").output
    assert "SCR has conflicting revision values: 'A' (row 4), 'B' (row 5)" in out


def test_inconsistent_expansion(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    path = write(
        tmp_path,
        "Level,Part Number,Rev,Description,Qty\n0,TOP,A,Top,\n1,SUB,A,Sub,1\n2,SCR,A,Screw,4\n"
        "1,BOX,A,Box,1\n2,SUB,A,Sub,1\n3,SCR,A,Screw,6\n",
    )
    assert "SUB is broken down differently here and at row 3" in cli.fail("import", path, "--new-ws", "ws").output


def test_duplicate_lines_are_summed(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    path = write(
        tmp_path,
        "Level,Part Number,Rev,Description,Qty,Ref Des\n0,TOP,A,Top,,\n1,R-10K,A,Resistor,2,R1-R2\n1,R-10K,A,Resistor,1,R5\n",
    )
    plan = plan_json(cli, path, "--new-ws", "ws")
    assert [(e["child"], e["quantity"], e["refdes"]) for e in plan["edges"]] == [("R-10K", "3", ["R1", "R2", "R5"])]
    assert any("quantities summed" in i["message"] for i in plan["issues"])


def test_same_revision_metadata_change(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    cli.run("import", write(tmp_path, "Level,PN,Rev,Description,Qty\n0,TOP,A,Top,\n1,SCR,A,Screw,4\n"), "--new-ws", "ws", "--apply")
    changed = write(tmp_path, "Level,PN,Rev,Description,Qty\n0,TOP,A,Top,\n1,SCR,A,Machine screw,4\n", "b.csv")
    out = cli.fail("import", changed).output
    assert "SCR rev A in the file differs from the current record (name) but has the same revision label" in out
    plan = plan_json(cli, changed, "--update-metadata", "--apply")
    assert plan["applied"] and plan["parts"][0]["action"] == "revise"


def test_invalid_part_number_needs_mapping(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    path = write(tmp_path, "Level,PN,Rev,Description,Qty\n0,TOP,A,Top,\n1,SCR M3/6,A,Screw,4\n")
    out = cli.fail("import", path, "--new-ws", "ws").output
    assert 'openbom map add "SCR M3/6" --part SCR-M3-6' in out
    cli.run("map", "add", "SCR M3/6", "--part", "SCR-M3-6")
    assert {p["part_id"] for p in plan_json(cli, path, "--new-ws", "ws")["parts"]} == {"TOP", "SCR-M3-6"}


def test_file_without_top_row_goes_under_workspace_root(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    cli.run("part", "add", "SRV", "--rev", "A", "--name", "Server")
    cli.run("ws", "create", "ws", "--root", "SRV")
    path = write(tmp_path, "Level,PN,Rev,Description,Qty\n1,CHS,A,Chassis,1\n2,SCR,A,Screw,4\n1,CPU,A,CPU,2\n")
    plan = plan_json(cli, path, "--apply")
    assert {(e["parent"], e["child"]) for e in plan["edges"]} == {("SRV", "CHS"), ("CHS", "SCR"), ("SRV", "CPU")}
    cli.run("commit", "-m", "x")
    _, root = root_configuration(cli.store(), "ws")
    assert len(root.entries) == 2


def test_top_row_must_match_workspace_root(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    cli.run("part", "add", "SRV", "--rev", "A", "--name", "Server")
    cli.run("ws", "create", "ws", "--root", "SRV")
    path = write(tmp_path, "Level,PN,Rev,Description,Qty\n0,OTHER,A,Other,\n1,SCR,A,Screw,4\n")
    assert "is not workspace ws's root acme:SRV" in cli.fail("import", path).output


def test_cannot_import_into_another_orgs_workspace(cli):
    setup_orgs(cli)
    import_psu(cli)
    cli.act("acme")
    out = cli.fail("import", PSU, "-w", "pwrco-psu-ws").output
    assert "workspace 'pwrco-psu-ws' is owned by org 'pwrco'" in out


def test_cycle_in_file_is_reported(cli, tmp_path):
    setup_orgs(cli)
    cli.act("acme")
    path = write(tmp_path, "Level,PN,Rev,Description,Qty\n0,TOP,A,Top,\n1,A1,A,A,1\n2,B1,A,B,1\n3,A1,A,A,1\n")
    out = cli.fail("import", path, "--new-ws", "ws").output
    assert "the resulting draft cannot be committed: cycle detected: acme:A1 -> acme:B1 -> acme:A1" in out


def test_xlsx_import(cli, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "EBOM"
    sheet.append(["Level", "Item Number", "Rev", "Description", "Qty", "UOM"])
    sheet.append([0, "TOP", "A", "Top", None, None])
    sheet.append([1, 100234, "B", "Numeric part number", 2.0, "Each"])
    sheet.append([1, "GLUE", "A", "Adhesive", 0.5, "ml"])
    path = tmp_path / "bom.xlsx"
    wb.save(path)
    setup_orgs(cli)
    cli.act("acme")
    plan = plan_json(cli, str(path), "--new-ws", "ws", "--sheet", "EBOM", "--apply")
    assert plan["sheet"] == "EBOM"
    assert sorted((e["child"], e["quantity"], e["uom"]) for e in plan["edges"]) == [("100234", "2", "ea"), ("GLUE", "0.5", "ml")]


# -- storage ------------------------------------------------------------------


def test_pre_release_database_is_refused_clearly(tmp_path):
    home = tmp_path / "old"
    home.mkdir()
    sqlite3.connect(home / "openbom.db").executescript("CREATE TABLE orgs (id TEXT); PRAGMA user_version = 3;")
    with pytest.raises(OpenBomError, match="pre-release build"):
        Store.open(home)


def test_new_database_is_current(tmp_path):
    store = Store.open(tmp_path, create=True)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
