"""SQLite storage (§7). All SQL in the project lives in this module.

Object reads go through exactly one method, Store.get_object(hash), so an
access check (D12) can be added in one place later.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import canonical
from .config import db_path
from .errors import OpenBomError

SCHEMA_VERSION = 4
# Versions 1-3 were pre-release development schemas (before "compositions" were
# renamed "configurations"); their object bytes can't be read by this version.
FIRST_RELEASED_VERSION = 4

SCHEMA = """
CREATE TABLE orgs (id TEXT PRIMARY KEY, name TEXT NOT NULL);

-- Immutable content-addressed objects. body = exact canonical bytes.
CREATE TABLE objects (
  hash TEXT PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('node','configuration','commit','correspondence')),
  owner_org_id TEXT NOT NULL REFERENCES orgs(id),
  body BLOB NOT NULL
);

-- Mutable pointer: current revision of each org part.
CREATE TABLE parts (
  org_id TEXT NOT NULL REFERENCES orgs(id),
  part_id TEXT NOT NULL,
  current_node TEXT NOT NULL REFERENCES objects(hash),
  PRIMARY KEY (org_id, part_id)
);
CREATE TABLE part_revisions (
  org_id TEXT NOT NULL, part_id TEXT NOT NULL,
  node TEXT NOT NULL REFERENCES objects(hash), created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, part_id, node)
);

CREATE TABLE workspaces (
  id TEXT PRIMARY KEY, owner_org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL, root_part_id TEXT NOT NULL,
  secret BLOB NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE refs (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  commit_hash TEXT NOT NULL REFERENCES objects(hash),
  PRIMARY KEY (workspace_id, name)
);

-- Draft (mutable). Own-part edges reference part IDs; external edges are pinned.
CREATE TABLE draft_edges (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  parent_part_id TEXT NOT NULL,
  child_key TEXT NOT NULL,
  child_part_id TEXT,
  ext_source_ws TEXT,
  ext_node TEXT, ext_configuration TEXT,
  quantity TEXT NOT NULL, uom TEXT NOT NULL DEFAULT 'ea',
  refdes TEXT,
  substitutes TEXT,                         -- JSON list of [part_id, priority|null]
  PRIMARY KEY (workspace_id, parent_part_id, child_key)
);

-- Import mappings: an org's persisted decisions about what a part number in an
-- imported BOM file means (rename to an own part, an external workspace, or ignore).
CREATE TABLE import_mappings (
  org_id TEXT NOT NULL REFERENCES orgs(id),
  source_pn TEXT NOT NULL,
  part_id TEXT,
  ext_source_ws TEXT,
  ignore INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (org_id, source_pn)
);

-- A reconciliation workspace pins two other workspaces and records how their parts correspond.
CREATE TABLE reconciliations (
  workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
  left_ws TEXT NOT NULL,
  right_ws TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '{}'        -- JSON: approver side, part-number prefixes to strip
);

-- Human decisions that steer the matcher, keyed by stable part keys so they carry to later revisions.
CREATE TABLE match_decisions (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  kind TEXT NOT NULL CHECK (kind IN ('pair','reject','accept')),
  left_part TEXT NOT NULL DEFAULT '',
  right_part TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (workspace_id, kind, left_part, right_part)
);
"""

# Migrations: MIGRATIONS[n] upgrades a database from version n to n + 1 (none yet).
MIGRATIONS: dict[int, str] = {}

MAIN_REF = "main"


@dataclass(frozen=True)
class Workspace:
    id: str
    owner_org_id: str
    name: str
    root_part_id: str
    secret: bytes
    created_at: str

    def __repr__(self) -> str:  # never leak the secret into logs/tracebacks
        return f"Workspace(id={self.id!r}, owner_org_id={self.owner_org_id!r}, root_part_id={self.root_part_id!r})"


@dataclass(frozen=True)
class DraftEdge:
    workspace_id: str
    parent_part_id: str
    child_key: str
    quantity: str
    uom: str
    refdes: tuple[str, ...] = ()
    child_part_id: str | None = None
    ext_source_ws: str | None = None
    ext_node: str | None = None
    ext_configuration: str | None = None
    substitutes: tuple[tuple[str, int | None], ...] = ()  # (own part_id, priority)

    @property
    def is_external(self) -> bool:
        return self.ext_source_ws is not None


@dataclass(frozen=True)
class Reconciliation:
    workspace_id: str
    left_ws: str
    right_ws: str
    options: dict[str, Any]


@dataclass(frozen=True)
class Mapping:
    """What a part number in an imported file means to an org. Exactly one target is set."""

    org_id: str
    source_pn: str
    part_id: str | None = None
    ext_source_ws: str | None = None
    ignore: bool = False


def own_child_key(part_id: str) -> str:
    return f"part:{part_id}"


def ext_child_key(source_ws: str) -> str:
    return f"ext:{source_ws}"


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._tx_depth = 0

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, home: Path, *, create: bool = False) -> "Store":
        path = db_path(home)
        if not path.exists() and not create:
            raise OpenBomError(f"no database at {path}; run `init` first (or pass --home)")
        home.mkdir(parents=True, exist_ok=True)
        store = cls(path)
        store.migrate()
        return store

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise OpenBomError(f"database schema version {version} is newer than this tool ({SCHEMA_VERSION})")
        if version == 0:
            self.conn.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            return
        if version < FIRST_RELEASED_VERSION:
            raise OpenBomError(
                f"{self.path} was created by a pre-release build (schema {version}) and can't be upgraded; "
                "delete it and re-import"
            )
        self.conn.execute("PRAGMA foreign_keys = OFF")  # migrations may rebuild tables; re-enabled below
        try:
            while version < SCHEMA_VERSION:
                self.conn.executescript(
                    f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version + 1}; COMMIT;"
                )
                version += 1
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block in one transaction. Nested calls join the outer one."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._tx_depth = 0

    @contextmanager
    def scratch(self) -> Iterator[None]:
        """Run a block in a transaction that is always rolled back (dry runs)."""
        if self._tx_depth:
            raise OpenBomError("a scratch transaction cannot be nested inside another transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield
        finally:
            self.conn.execute("ROLLBACK")
            self._tx_depth = 0

    # -- objects -----------------------------------------------------------

    def put_object(self, obj_type: str, owner_org: str, data: bytes) -> str:
        """Store canonical bytes; an existing hash is a no-op."""
        h = canonical.hash_bytes(data)
        self.conn.execute(
            "INSERT OR IGNORE INTO objects (hash, type, owner_org_id, body) VALUES (?, ?, ?, ?)",
            (h, obj_type, owner_org, data),
        )
        return h

    def get_object(self, h: str) -> bytes | None:
        """The single read path for object bytes (D12: permission check goes here)."""
        row = self.conn.execute("SELECT body FROM objects WHERE hash = ?", (h,)).fetchone()
        return None if row is None else bytes(row["body"])

    def object_type(self, h: str) -> str | None:
        row = self.conn.execute("SELECT type FROM objects WHERE hash = ?", (h,)).fetchone()
        return None if row is None else row["type"]

    def find_objects_by_prefix(self, prefix: str, obj_type: str | None = None) -> list[str]:
        sql = "SELECT hash FROM objects WHERE hash LIKE ? ESCAPE '\\'"
        args: list[Any] = [prefix.replace("%", "\\%").replace("_", "\\_") + "%"]
        if obj_type is not None:
            sql += " AND type = ?"
            args.append(obj_type)
        return [r["hash"] for r in self.conn.execute(sql + " ORDER BY hash LIMIT 20", args)]

    # -- orgs --------------------------------------------------------------

    def add_org(self, org_id: str, name: str) -> None:
        self.conn.execute("INSERT INTO orgs (id, name) VALUES (?, ?)", (org_id, name))

    def get_org(self, org_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT id, name FROM orgs WHERE id = ?", (org_id,)).fetchone()

    def list_orgs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT id, name FROM orgs ORDER BY id"))

    # -- parts -------------------------------------------------------------

    def get_part_node(self, org_id: str, part_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT current_node FROM parts WHERE org_id = ? AND part_id = ?", (org_id, part_id)
        ).fetchone()
        return None if row is None else row["current_node"]

    def set_part_node(self, org_id: str, part_id: str, node: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO parts (org_id, part_id, current_node) VALUES (?, ?, ?) "
            "ON CONFLICT (org_id, part_id) DO UPDATE SET current_node = excluded.current_node",
            (org_id, part_id, node),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO part_revisions (org_id, part_id, node, created_at) VALUES (?, ?, ?, ?)",
            (org_id, part_id, node, created_at),
        )

    def list_parts(self, org_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT org_id, part_id, current_node FROM parts"
        args: tuple[Any, ...] = ()
        if org_id is not None:
            sql += " WHERE org_id = ?"
            args = (org_id,)
        return list(self.conn.execute(sql + " ORDER BY org_id, part_id", args))

    def part_revisions(self, org_id: str, part_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT node, created_at FROM part_revisions WHERE org_id = ? AND part_id = ? "
                "ORDER BY created_at, rowid",
                (org_id, part_id),
            )
        )

    # -- workspaces and refs ----------------------------------------------

    def add_workspace(self, ws: Workspace) -> None:
        self.conn.execute(
            "INSERT INTO workspaces (id, owner_org_id, name, root_part_id, secret, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ws.id, ws.owner_org_id, ws.name, ws.root_part_id, ws.secret, ws.created_at),
        )

    def get_workspace(self, ws_id: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        return None if row is None else _workspace(row)

    def list_workspaces(self) -> list[Workspace]:
        return [_workspace(r) for r in self.conn.execute("SELECT * FROM workspaces ORDER BY id")]

    def get_ref(self, ws_id: str, name: str = MAIN_REF) -> str | None:
        row = self.conn.execute(
            "SELECT commit_hash FROM refs WHERE workspace_id = ? AND name = ?", (ws_id, name)
        ).fetchone()
        return None if row is None else row["commit_hash"]

    def set_ref(self, ws_id: str, commit_hash: str, name: str = MAIN_REF) -> None:
        self.conn.execute(
            "INSERT INTO refs (workspace_id, name, commit_hash) VALUES (?, ?, ?) "
            "ON CONFLICT (workspace_id, name) DO UPDATE SET commit_hash = excluded.commit_hash",
            (ws_id, name, commit_hash),
        )

    # -- draft edges -------------------------------------------------------

    def list_draft_edges(self, ws_id: str) -> list[DraftEdge]:
        rows = self.conn.execute(
            "SELECT * FROM draft_edges WHERE workspace_id = ? ORDER BY parent_part_id, child_key", (ws_id,)
        )
        return [_draft_edge(r) for r in rows]

    def get_draft_edge(self, ws_id: str, parent_part_id: str, child_key: str) -> DraftEdge | None:
        row = self.conn.execute(
            "SELECT * FROM draft_edges WHERE workspace_id = ? AND parent_part_id = ? AND child_key = ?",
            (ws_id, parent_part_id, child_key),
        ).fetchone()
        return None if row is None else _draft_edge(row)

    def put_draft_edge(self, edge: DraftEdge) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO draft_edges (workspace_id, parent_part_id, child_key, child_part_id, "
            "ext_source_ws, ext_node, ext_configuration, quantity, uom, refdes, substitutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge.workspace_id,
                edge.parent_part_id,
                edge.child_key,
                edge.child_part_id,
                edge.ext_source_ws,
                edge.ext_node,
                edge.ext_configuration,
                edge.quantity,
                edge.uom,
                json.dumps(list(edge.refdes)) if edge.refdes else None,
                json.dumps([list(x) for x in edge.substitutes]) if edge.substitutes else None,
            ),
        )

    def delete_draft_edge(self, ws_id: str, parent_part_id: str, child_key: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM draft_edges WHERE workspace_id = ? AND parent_part_id = ? AND child_key = ?",
            (ws_id, parent_part_id, child_key),
        )
        return cur.rowcount > 0

    def delete_all_draft_edges(self, ws_id: str) -> int:
        return self.conn.execute("DELETE FROM draft_edges WHERE workspace_id = ?", (ws_id,)).rowcount

    # -- reconciliations ---------------------------------------------------

    def add_reconciliation(self, ws_id: str, left_ws: str, right_ws: str, options: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO reconciliations (workspace_id, left_ws, right_ws, options) VALUES (?, ?, ?, ?)",
            (ws_id, left_ws, right_ws, json.dumps(options, sort_keys=True)),
        )

    def get_reconciliation(self, ws_id: str) -> Reconciliation | None:
        row = self.conn.execute("SELECT * FROM reconciliations WHERE workspace_id = ?", (ws_id,)).fetchone()
        if row is None:
            return None
        return Reconciliation(row["workspace_id"], row["left_ws"], row["right_ws"], json.loads(row["options"]))

    def put_decision(self, ws_id: str, kind: str, left_part: str, right_part: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO match_decisions (workspace_id, kind, left_part, right_part, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (ws_id, kind, left_part, right_part, note),
        )

    def delete_decision(self, ws_id: str, kind: str, left_part: str, right_part: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM match_decisions WHERE workspace_id = ? AND kind = ? AND left_part = ? AND right_part = ?",
            (ws_id, kind, left_part, right_part),
        )
        return cur.rowcount > 0

    def list_decisions(self, ws_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT kind, left_part, right_part, note FROM match_decisions WHERE workspace_id = ? "
                "ORDER BY kind, left_part, right_part",
                (ws_id,),
            )
        )

    # -- import mappings ---------------------------------------------------

    def get_mapping(self, org_id: str, source_pn: str) -> Mapping | None:
        row = self.conn.execute(
            "SELECT * FROM import_mappings WHERE org_id = ? AND source_pn = ?", (org_id, source_pn)
        ).fetchone()
        return None if row is None else _mapping(row)

    def list_mappings(self, org_id: str) -> list[Mapping]:
        rows = self.conn.execute("SELECT * FROM import_mappings WHERE org_id = ? ORDER BY source_pn", (org_id,))
        return [_mapping(r) for r in rows]

    def put_mapping(self, mapping: Mapping) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO import_mappings (org_id, source_pn, part_id, ext_source_ws, ignore) "
            "VALUES (?, ?, ?, ?, ?)",
            (mapping.org_id, mapping.source_pn, mapping.part_id, mapping.ext_source_ws, int(mapping.ignore)),
        )

    def delete_mapping(self, org_id: str, source_pn: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM import_mappings WHERE org_id = ? AND source_pn = ?", (org_id, source_pn)
        )
        return cur.rowcount > 0


def _mapping(row: sqlite3.Row) -> Mapping:
    return Mapping(
        org_id=row["org_id"],
        source_pn=row["source_pn"],
        part_id=row["part_id"],
        ext_source_ws=row["ext_source_ws"],
        ignore=bool(row["ignore"]),
    )


def _workspace(row: sqlite3.Row) -> Workspace:
    return Workspace(
        id=row["id"],
        owner_org_id=row["owner_org_id"],
        name=row["name"],
        root_part_id=row["root_part_id"],
        secret=bytes(row["secret"]),
        created_at=row["created_at"],
    )


def _draft_edge(row: sqlite3.Row) -> DraftEdge:
    return DraftEdge(
        workspace_id=row["workspace_id"],
        parent_part_id=row["parent_part_id"],
        child_key=row["child_key"],
        child_part_id=row["child_part_id"],
        ext_source_ws=row["ext_source_ws"],
        ext_node=row["ext_node"],
        ext_configuration=row["ext_configuration"],
        quantity=row["quantity"],
        uom=row["uom"],
        refdes=tuple(json.loads(row["refdes"])) if row["refdes"] else (),
        substitutes=tuple((p, prio) for p, prio in json.loads(row["substitutes"])) if row["substitutes"] else (),
    )

