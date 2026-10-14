"""§11 Within-org history (tests 4-9)."""

import json

from conftest import build_acme_server, child_configuration, entry_for, root_configuration

from openbom.workspace import log

WS = "acme-server-ws"


# 4
def test_tree_renders(cli):
    build_acme_server(cli)
    out = cli.run("tree").stdout
    lines = out.splitlines()
    assert lines[0].startswith('acme:SRV rev A  "1U compute server"  [sha256:')
    assert lines[1].startswith('├── 1 ea  acme:CHS rev A  "1U chassis"  [sha256:')
    assert lines[2] == '│   └── 8 ea  acme:SCR rev A  "M3x6 screw"  (mfr: Acme Fasteners, mpn: M3-6-SS)'
    assert lines[3].startswith('└── 1 ea  acme:MB rev A  "Motherboard"  [sha256:')
    assert lines[4] == '    ├── 2 ea  acme:CPU rev A  "CPU"  (mpn: X1, refdes: CPU0,CPU1)'
    assert lines[5] == '    └── 8 ea  acme:DIMM rev A  "32GB DIMM"'
    assert len(lines) == 6

    depth1 = cli.run("tree", "--depth", "1").stdout.splitlines()
    assert len(depth1) == 3 and "SCR" not in "".join(depth1)

    tree = json.loads(cli.run("tree", "--json").stdout)
    assert tree["part"] == "acme:SRV"
    assert [c["part"] for c in tree["children"]] == ["acme:CHS", "acme:MB"]
    assert tree["children"][0]["children"][0]["quantity"] == "8"


# 5
def test_leaf_revision_propagates_to_ancestors_only(cli):
    build_acme_server(cli)
    store = cli.store()
    root1, comp1 = root_configuration(store, WS)
    chs1, _ = child_configuration(store, comp1, "acme:CHS")
    mb1, _ = child_configuration(store, comp1, "acme:MB")
    scr1 = entry_for(store, child_configuration(store, comp1, "acme:CHS")[1], "acme:SCR").node

    cli.run("part", "revise", "SCR", "--rev", "B", "--mpn", "M3-6-SS-B")
    cli.run("commit", "-m", "screw rev B")

    root2, comp2 = root_configuration(store, WS)
    chs2, chs_comp2 = child_configuration(store, comp2, "acme:CHS")
    mb2, _ = child_configuration(store, comp2, "acme:MB")
    scr2 = entry_for(store, chs_comp2, "acme:SCR").node
    assert scr1 != scr2
    assert chs1 != chs2
    assert root1 != root2
    assert mb1 == mb2  # sibling sub-assembly untouched
    assert comp1.node == comp2.node  # the root part itself was not revised


# 6
def test_log_and_diff_one_revised_part(cli):
    build_acme_server(cli)
    cli.run("part", "revise", "SCR", "--rev", "B")
    cli.run("commit", "-m", "screw rev B")

    out = cli.run("log").stdout
    assert out.count("commit sha256:") == 2
    assert out.index("screw rev B") < out.index("initial server")
    assert len(log(cli.store(), WS)) == 2

    d = json.loads(cli.run("diff", "HEAD~1", "HEAD", "--json").stdout)
    assert [p["part"] for p in d["revised_parts"]] == ["acme:SCR"]
    assert d["revised_parts"][0]["old_revision"] == "A"
    assert d["revised_parts"][0]["new_revision"] == "B"
    assert d["revised_parts"][0]["fields"] == ["revision"]
    assert d["added_parts"] == d["removed_parts"] == d["edge_changes"] == d["opaque_changes"] == []

    text = cli.run("diff").stdout  # defaults to HEAD~1 HEAD
    assert "acme:SCR rev A -> B" in text


# 7
def test_shared_subassembly_stored_once_and_rendered_once(cli):
    build_acme_server(cli)
    cli.run("part", "add", "BRKT", "--rev", "A", "--name", "Bracket assembly")
    cli.run("part", "add", "RIVET", "--rev", "A", "--name", "Rivet")
    cli.run("edge", "add", "BRKT", "RIVET", "--qty", "4")
    cli.run("edge", "add", "CHS", "BRKT", "--qty", "2")
    cli.run("edge", "add", "MB", "BRKT", "--qty", "1")
    cli.run("commit", "-m", "shared bracket")

    store = cli.store()
    _, root = root_configuration(store, WS)
    _, chs = child_configuration(store, root, "acme:CHS")
    _, mb = child_configuration(store, root, "acme:MB")
    assert entry_for(store, chs, "acme:BRKT").configuration == entry_for(store, mb, "acme:BRKT").configuration

    out = cli.run("tree").stdout
    assert out.count("Rivet") == 1
    assert out.count("(see above)") == 1


# 8
def test_cycle_rejected_at_commit_with_path(cli):
    build_acme_server(cli)
    cli.run("edge", "add", "SCR", "CHS", "--qty", "1")  # accepted into the draft...
    result = cli.fail("commit", "-m", "cycle")  # ...rejected at commit
    assert "cycle detected: acme:CHS -> acme:SCR -> acme:CHS" in result.output


# 9
def test_nothing_to_commit(cli):
    build_acme_server(cli)
    assert "nothing to commit" in cli.fail("commit", "-m", "again").output


def test_unreachable_edges_warn_but_commit(cli):
    build_acme_server(cli)
    cli.run("part", "add", "LOOSE", "--rev", "A", "--name", "Loose assembly")
    cli.run("edge", "add", "LOOSE", "SCR", "--qty", "1")
    cli.run("edge", "set", "CHS", "SCR", "--qty", "6")
    result = cli.run("commit", "-m", "fewer screws")
    assert "not reachable" in result.output
    d = json.loads(cli.run("diff", "--json").stdout)
    assert [(e["parent"], e["child"], e["fields"]) for e in d["edge_changes"]] == [("acme:CHS", "acme:SCR", ["quantity"])]


def test_revision_references(cli):
    build_acme_server(cli)
    head_hash = cli.run("log", "-n", "1").stdout.split()[1]
    hexpart = head_hash.removeprefix("sha256:")
    assert cli.run("show", hexpart[:8]).stdout.startswith(f"commit {head_hash}")
    assert cli.run("show", head_hash).stdout.startswith(f"commit {head_hash}")
    assert "is not a valid revision" in cli.fail("show", hexpart[:7]).output
    assert "only 1 commit" in cli.fail("tree", "HEAD~1").output
    assert cli.run("cat-object", head_hash).stdout.strip().startswith('{"author":')
