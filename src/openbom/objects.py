"""The four content-addressed object types (§4): Node, Entry, Configuration, Commit.

Two ways in:
- make_* constructors validate and *normalize* (sort unordered lists, drop
  empty optionals). All object creation goes through them; in particular all
  node construction goes through make_node, so the hashed field set can change
  in one place (D20).
- parse_object / decode validate *strictly*: a stored body must already be in
  normalized form (we rebuild it and compare), otherwise it is a SchemaError.
  verify relies on this.

Bodies are plain dicts in the restricted JSON value set (no floats).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Union

from . import canonical
from .errors import SchemaError

SCHEMA_VERSION = 1
NODE_KINDS = ("part",)  # D1: physical/serialized instances will add kinds.
RELS = ("contains",)  # D4: alternates, supersedes, built-from, ...
# D3: stored, not resolved. "customer" and "supplier" hold the other party's part number
# for the same item (cross-references that reconciliation matches on).
IDENTIFIER_SCHEMES = ("gtin", "jep106", "mpn", "customer", "supplier", "other")
DEFAULT_UOM = "ea"

ORG_ID_RE = re.compile(r"[a-z0-9\-]{1,32}")
PART_ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,64}")
# TODO(spec): the spec does not constrain workspace IDs; reuse the part ID
# pattern (which also guarantees no ':' so 'ext:<ws>' child keys stay unambiguous).
WORKSPACE_ID_RE = PART_ID_RE
QUANTITY_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
SALT_RE = re.compile(r"[0-9a-f]{64}")
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


# --------------------------------------------------------------------------
# Field validators
# --------------------------------------------------------------------------


def validate_org_id(value: Any) -> str:
    if not isinstance(value, str) or not ORG_ID_RE.fullmatch(value):
        raise SchemaError(f"invalid org ID {value!r}: must match ^[a-z0-9-]{{1,32}}$")
    return value


def validate_part_id(value: Any) -> str:
    if not isinstance(value, str) or not PART_ID_RE.fullmatch(value):
        raise SchemaError(f"invalid part ID {value!r}: must match ^[A-Za-z0-9._-]{{1,64}}$")
    return value


def validate_workspace_id(value: Any) -> str:
    if not isinstance(value, str) or not WORKSPACE_ID_RE.fullmatch(value):
        raise SchemaError(f"invalid workspace ID {value!r}: must match ^[A-Za-z0-9._-]{{1,64}}$")
    return value


def validate_quantity(value: Any) -> str:
    if not isinstance(value, str) or not QUANTITY_RE.fullmatch(value) or Decimal(value) <= 0:
        raise SchemaError(f"invalid quantity {value!r}: must be a positive decimal string like '2' or '0.5'")
    return value


def validate_hash(value: Any, what: str) -> str:
    if not canonical.is_hash(value):
        raise SchemaError(f"{what} must be a hash 'sha256:<64 hex>', got {value!r}")
    return value


def _nonempty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _str_map(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{what} must be an object of string to string")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise SchemaError(f"{what} must map strings to strings (bad entry {k!r}: {v!r})")
    return dict(value)


def _str_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise SchemaError(f"{what} must be a list of non-empty strings")
    return list(value)


def utc_timestamp(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Node (§4.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    org: str
    part_id: str
    revision: str
    name: str
    kind: str = "part"
    description: str | None = None
    manufacturer: str | None = None
    mpn: str | None = None
    identifiers: tuple[tuple[str, str], ...] = ()
    uris: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    # Approved manufacturer list: every (manufacturer, mpn) approved for this part, when the
    # source provides a list. manufacturer/mpn above hold the primary. Hashed: a sourcing
    # change is a controlled change.
    aml: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.org}:{self.part_id}"

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "node",
            "v": SCHEMA_VERSION,
            "kind": self.kind,
            "org": self.org,
            "part_id": self.part_id,
            "revision": self.revision,
            "name": self.name,
        }
        for key in ("description", "manufacturer", "mpn"):
            value = getattr(self, key)
            if value is not None:
                body[key] = value
        if self.aml:
            body["aml"] = [{"manufacturer": m, "mpn": p} for m, p in self.aml]
        if self.identifiers:
            body["identifiers"] = [{"scheme": s, "value": v} for s, v in self.identifiers]
        if self.uris:
            body["uris"] = list(self.uris)
        if self.attributes:
            body["attributes"] = dict(self.attributes)
        return body


# Top-level node fields compared by diff (§8.3) to say "what changed".
NODE_DESCRIPTIVE_FIELDS = (
    "kind", "revision", "name", "description", "manufacturer", "mpn", "aml", "identifiers", "uris",
)


def make_node(
    *,
    org: str,
    part_id: str,
    revision: str,
    name: str,
    kind: str = "part",
    description: str | None = None,
    manufacturer: str | None = None,
    mpn: str | None = None,
    identifiers: Iterable[tuple[str, str]] = (),
    uris: Iterable[str] = (),
    attributes: Mapping[str, str] | None = None,
    aml: Iterable[tuple[str, str]] = (),
) -> Node:
    """The single constructor for node objects (D20 seam)."""
    if kind not in NODE_KINDS:
        raise SchemaError(f"invalid node kind {kind!r}: only {', '.join(NODE_KINDS)} is valid in v1")
    validate_org_id(org)
    validate_part_id(part_id)
    _nonempty_str(revision, "revision")
    _nonempty_str(name, "name")
    for key, value in (("description", description), ("manufacturer", manufacturer), ("mpn", mpn)):
        if value is not None and not isinstance(value, str):
            raise SchemaError(f"{key} must be a string")
    ids = set()
    for item in identifiers:
        scheme, value = item
        if scheme not in IDENTIFIER_SCHEMES:
            raise SchemaError(
                f"invalid identifier scheme {scheme!r}: valid schemes are {', '.join(IDENTIFIER_SCHEMES)}"
            )
        _nonempty_str(value, f"identifier value for scheme {scheme!r}")
        ids.add((scheme, value))
    approved = set()
    for manufacturer_name, part_number in aml:
        _nonempty_str(manufacturer_name, "aml manufacturer")
        _nonempty_str(part_number, "aml mpn")
        approved.add((manufacturer_name, part_number))
    return Node(
        org=org,
        part_id=part_id,
        revision=revision,
        name=name,
        kind=kind,
        description=description,
        manufacturer=manufacturer,
        mpn=mpn,
        identifiers=tuple(sorted(ids)),
        uris=tuple(sorted(set(_str_list(list(uris), "uris")))),
        attributes=_str_map(attributes or {}, "attributes"),
        aml=tuple(sorted(approved)),
    )


def _parse_node(body: dict[str, Any]) -> Node:
    _check_keys(
        body,
        required={"type", "v", "kind", "org", "part_id", "revision", "name"},
        optional={"description", "manufacturer", "mpn", "aml", "identifiers", "uris", "attributes"},
        what="node",
    )
    aml = body.get("aml", [])
    if not isinstance(aml, list) or not all(isinstance(a, dict) and set(a) == {"manufacturer", "mpn"} for a in aml):
        raise SchemaError("node aml must be a list of objects with exactly 'manufacturer' and 'mpn'")
    identifiers = body.get("identifiers", [])
    if not isinstance(identifiers, list):
        raise SchemaError("node identifiers must be a list")
    pairs = []
    for item in identifiers:
        if not isinstance(item, dict) or set(item) != {"scheme", "value"}:
            raise SchemaError("each identifier must be an object with exactly 'scheme' and 'value'")
        pairs.append((item["scheme"], item["value"]))
    return make_node(
        org=body["org"],
        part_id=body["part_id"],
        revision=body["revision"],
        name=body["name"],
        kind=body["kind"],
        description=body.get("description"),
        manufacturer=body.get("manufacturer"),
        mpn=body.get("mpn"),
        identifiers=pairs,
        uris=_str_list(body.get("uris", []), "uris"),
        attributes=_str_map(body.get("attributes", {}), "attributes"),
        aml=[(a["manufacturer"], a["mpn"]) for a in aml],
    )


# --------------------------------------------------------------------------
# Entry (§4.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    node: str
    quantity: str
    uom: str = DEFAULT_UOM
    configuration: str | None = None
    source: str | None = None
    refdes: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    rel: str = "contains"
    # Line-level substitutes: other parts that may fill this same position, as
    # (node hash, priority). They don't add quantity. Part-level manufacturer
    # alternatives belong in the child node's aml instead.
    substitutes: tuple[tuple[str, int | None], ...] = ()

    @property
    def is_external(self) -> bool:
        return self.source is not None

    def sort_key(self) -> tuple[str, str, str]:
        return (self.node, self.source or "", self.configuration or "")

    def body(self) -> dict[str, Any]:
        # TODO(spec): uom has a default ("ea") but the spec doesn't say whether
        # to omit it when defaulted. We always include it so entries are explicit.
        body: dict[str, Any] = {"rel": self.rel, "node": self.node}
        if self.configuration is not None:
            body["configuration"] = self.configuration
        if self.source is not None:
            body["source"] = self.source
        body["quantity"] = self.quantity
        body["uom"] = self.uom
        if self.refdes:
            body["refdes"] = list(self.refdes)
        if self.substitutes:
            body["substitutes"] = [
                {"node": n, **({"priority": p} if p is not None else {})} for n, p in self.substitutes
            ]
        if self.attributes:
            body["attributes"] = dict(self.attributes)
        return body


def _substitute_key(item: tuple[str, int | None]) -> tuple[int, int, str]:
    node, priority = item
    return (0 if priority is not None else 1, priority or 0, node)


def make_entry(
    *,
    node: str,
    quantity: str,
    uom: str = DEFAULT_UOM,
    configuration: str | None = None,
    source: str | None = None,
    refdes: Iterable[str] = (),
    attributes: Mapping[str, str] | None = None,
    rel: str = "contains",
    substitutes: Iterable[tuple[str, int | None]] = (),
) -> Entry:
    if rel not in RELS:
        raise SchemaError(f"invalid entry rel {rel!r}: only {', '.join(RELS)} is valid in v1")
    validate_hash(node, "entry node")
    if configuration is not None:
        validate_hash(configuration, "entry configuration")
    if source is not None:
        validate_workspace_id(source)
    validate_quantity(quantity)
    _nonempty_str(uom, "uom")
    subs: dict[str, int | None] = {}
    for sub_node, priority in substitutes:
        validate_hash(sub_node, "substitute node")
        if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int) or priority < 1):
            raise SchemaError(f"substitute priority must be a positive integer, got {priority!r}")
        if sub_node == node:
            raise SchemaError("a part cannot be its own substitute")
        if sub_node in subs:
            raise SchemaError(f"duplicate substitute {sub_node}")
        subs[sub_node] = priority
    # TODO(spec): refdes sort order is plain string order (so "R10" < "R2"), not natural order.
    return Entry(
        rel=rel,
        node=node,
        configuration=configuration,
        source=source,
        quantity=quantity,
        uom=uom,
        refdes=tuple(sorted(set(_str_list(list(refdes), "refdes")))),
        attributes=_str_map(attributes or {}, "entry attributes"),
        substitutes=tuple(sorted(subs.items(), key=_substitute_key)),
    )


def _parse_entry(body: Any) -> Entry:
    if not isinstance(body, dict):
        raise SchemaError("configuration entries must be objects")
    _check_keys(
        body,
        required={"rel", "node", "quantity", "uom"},
        optional={"configuration", "source", "refdes", "attributes", "substitutes"},
        what="entry",
    )
    subs = body.get("substitutes", [])
    if not isinstance(subs, list) or not all(
        isinstance(x, dict) and "node" in x and set(x) <= {"node", "priority"} for x in subs
    ):
        raise SchemaError("entry substitutes must be a list of objects with 'node' and optional 'priority'")
    return make_entry(
        rel=body["rel"],
        node=body["node"],
        configuration=body.get("configuration"),
        source=body.get("source"),
        quantity=body["quantity"],
        uom=body["uom"],
        refdes=_str_list(body.get("refdes", []), "refdes"),
        attributes=_str_map(body.get("attributes", {}), "entry attributes"),
        substitutes=[(x["node"], x.get("priority")) for x in subs],
    )


# --------------------------------------------------------------------------
# Configuration (§4.3, §4.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Configuration:
    node: str
    entries: tuple[Entry, ...]
    salt: str

    def unsalted_body(self) -> dict[str, Any]:
        return _unsalted_body(self.node, self.entries)

    def body(self) -> dict[str, Any]:
        body = self.unsalted_body()
        body["salt"] = self.salt
        return body


def _unsalted_body(node: str, entries: Iterable[Entry]) -> dict[str, Any]:
    return {
        "type": "configuration",
        "v": SCHEMA_VERSION,
        "node": node,
        "entries": [e.body() for e in entries],
    }


def compute_salt(secret: bytes, node: str, entries: Iterable[Entry]) -> str:
    """salt = hex(HMAC-SHA256(workspace.secret, JCS(unsalted body))) (§4.4)."""
    return canonical.hmac_hex(secret, canonical.canonical_bytes(_unsalted_body(node, entries)))


def _sorted_entries(entries: Iterable[Entry]) -> tuple[Entry, ...]:
    ordered = tuple(sorted(entries, key=Entry.sort_key))
    seen: set[tuple[str, str]] = set()
    for e in ordered:
        key = (e.node, e.source or "")
        if key in seen:
            raise SchemaError(
                f"duplicate entry for node {canonical.short_hash(e.node)}"
                + (f" from {e.source}" if e.source else "")
                + "; use quantity for multiples"
            )
        seen.add(key)
    return ordered


def make_configuration(*, node: str, entries: Iterable[Entry], secret: bytes) -> Configuration:
    validate_hash(node, "configuration node")
    ordered = _sorted_entries(entries)
    return Configuration(node=node, entries=ordered, salt=compute_salt(secret, node, ordered))


def _parse_configuration(body: dict[str, Any]) -> Configuration:
    _check_keys(body, required={"type", "v", "node", "entries", "salt"}, optional=set(), what="configuration")
    validate_hash(body["node"], "configuration node")
    if not isinstance(body["entries"], list):
        raise SchemaError("configuration entries must be a list")
    salt = body["salt"]
    if not isinstance(salt, str) or not SALT_RE.fullmatch(salt):
        raise SchemaError("configuration salt must be 64 lowercase hex characters")
    entries = _sorted_entries(_parse_entry(e) for e in body["entries"])
    return Configuration(node=body["node"], entries=entries, salt=salt)


# --------------------------------------------------------------------------
# Commit (§4.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    workspace: str
    configuration: str
    parents: tuple[str, ...]
    author_org: str
    timestamp: str
    message: str
    author_user: str | None = None
    # Reconciliation commits also point at the correspondence they record.
    correspondence: str | None = None

    def body(self) -> dict[str, Any]:
        author = {"org": self.author_org}
        if self.author_user is not None:
            author["user"] = self.author_user
        body = {
            "type": "commit",
            "v": SCHEMA_VERSION,
            "workspace": self.workspace,
            "configuration": self.configuration,
            "parents": list(self.parents),
            "author": author,
            "timestamp": self.timestamp,
            "message": self.message,
        }
        if self.correspondence is not None:
            body["correspondence"] = self.correspondence
        return body


def make_commit(
    *,
    workspace: str,
    configuration: str,
    parents: Iterable[str],
    author_org: str,
    message: str,
    author_user: str | None = None,
    timestamp: str | None = None,
    correspondence: str | None = None,
) -> Commit:
    validate_workspace_id(workspace)
    validate_hash(configuration, "commit configuration")
    parents = tuple(parents)
    if len(parents) > 1:
        raise SchemaError("v1 commits have at most one parent (merges are deferred)")
    for p in parents:
        validate_hash(p, "commit parent")
    validate_org_id(author_org)
    if author_user is not None:
        _nonempty_str(author_user, "author.user")
    timestamp = timestamp or utc_timestamp()
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
        raise SchemaError(f"invalid timestamp {timestamp!r}: must be UTC 'YYYY-MM-DDTHH:MM:SSZ'")
    if not isinstance(message, str):
        raise SchemaError("commit message must be a string")
    if correspondence is not None:
        validate_hash(correspondence, "commit correspondence")
    return Commit(
        workspace=workspace,
        configuration=configuration,
        parents=parents,
        author_org=author_org,
        author_user=author_user,
        timestamp=timestamp,
        message=message,
        correspondence=correspondence,
    )


def _parse_commit(body: dict[str, Any]) -> Commit:
    _check_keys(
        body,
        required={"type", "v", "workspace", "configuration", "parents", "author", "timestamp", "message"},
        optional={"correspondence"},
        what="commit",
    )
    author = body["author"]
    if not isinstance(author, dict) or "org" not in author or set(author) - {"org", "user"}:
        raise SchemaError("commit author must be an object with 'org' and optional 'user'")
    if not isinstance(body["parents"], list):
        raise SchemaError("commit parents must be a list")
    return make_commit(
        workspace=body["workspace"],
        configuration=body["configuration"],
        parents=body["parents"],
        author_org=author["org"],
        author_user=author.get("user"),
        timestamp=body["timestamp"],
        message=body["message"],
        correspondence=body.get("correspondence"),
    )


# --------------------------------------------------------------------------
# Correspondence (reconciliation)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    left: str  # node hash on the left side
    right: str  # node hash on the right side
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Correspondence:
    """The agreed pairing between two pinned configurations (e.g., a customer's BOM
    and an ODM's BOM of the same product), plus one-sided items accepted as known
    differences. Findings (quantity checks etc.) are recomputed from this and the
    two graphs, so they are not stored.
    """

    left_source: str
    left_configuration: str
    right_source: str
    right_configuration: str
    pairs: tuple[Pair, ...]
    accepted: tuple[tuple[str, str], ...] = ()  # (node hash, note)

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "correspondence",
            "v": SCHEMA_VERSION,
            "left": {"source": self.left_source, "configuration": self.left_configuration},
            "right": {"source": self.right_source, "configuration": self.right_configuration},
            "pairs": [{"left": p.left, "right": p.right, "evidence": list(p.evidence)} for p in self.pairs],
        }
        if self.accepted:
            body["accepted"] = [{"node": n, "note": note} for n, note in self.accepted]
        return body


def make_correspondence(
    *,
    left_source: str,
    left_configuration: str,
    right_source: str,
    right_configuration: str,
    pairs: Iterable[tuple[str, str, Iterable[str]]],
    accepted: Iterable[tuple[str, str]] = (),
) -> Correspondence:
    for ws, comp, side in ((left_source, left_configuration, "left"), (right_source, right_configuration, "right")):
        validate_workspace_id(ws)
        validate_hash(comp, f"{side} configuration")
    built: dict[tuple[str, str], Pair] = {}
    for left, right, evidence in pairs:
        validate_hash(left, "pair left node")
        validate_hash(right, "pair right node")
        ev = tuple(sorted(set(_str_list(list(evidence), "pair evidence"))))
        if not ev:
            raise SchemaError("every pair needs at least one kind of evidence")
        if (left, right) in built:
            raise SchemaError(f"duplicate pair {left} / {right}")
        built[(left, right)] = Pair(left, right, ev)
    notes: dict[str, str] = {}
    for node, note in accepted:
        validate_hash(node, "accepted node")
        if not isinstance(note, str):
            raise SchemaError("accepted note must be a string")
        if node in notes:
            raise SchemaError(f"node {node} accepted twice")
        notes[node] = note
    return Correspondence(
        left_source=left_source,
        left_configuration=left_configuration,
        right_source=right_source,
        right_configuration=right_configuration,
        pairs=tuple(built[k] for k in sorted(built)),
        accepted=tuple(sorted(notes.items())),
    )


def _parse_correspondence(body: dict[str, Any]) -> Correspondence:
    _check_keys(body, required={"type", "v", "left", "right", "pairs"}, optional={"accepted"}, what="correspondence")
    for side in ("left", "right"):
        if not isinstance(body[side], dict) or set(body[side]) != {"source", "configuration"}:
            raise SchemaError(f"correspondence {side} must be an object with 'source' and 'configuration'")
    pairs = body["pairs"]
    if not isinstance(pairs, list) or not all(isinstance(p, dict) and set(p) == {"left", "right", "evidence"} for p in pairs):
        raise SchemaError("correspondence pairs must be objects with 'left', 'right', and 'evidence'")
    accepted = body.get("accepted", [])
    if not isinstance(accepted, list) or not all(isinstance(a, dict) and set(a) == {"node", "note"} for a in accepted):
        raise SchemaError("correspondence accepted entries must be objects with 'node' and 'note'")
    return make_correspondence(
        left_source=body["left"]["source"],
        left_configuration=body["left"]["configuration"],
        right_source=body["right"]["source"],
        right_configuration=body["right"]["configuration"],
        pairs=[(p["left"], p["right"], p["evidence"]) for p in pairs],
        accepted=[(a["node"], a["note"]) for a in accepted],
    )


# --------------------------------------------------------------------------
# Generic parsing
# --------------------------------------------------------------------------

StoredObject = Union[Node, Configuration, Commit, Correspondence]

_PARSERS = {
    "node": _parse_node,
    "configuration": _parse_configuration,
    "commit": _parse_commit,
    "correspondence": _parse_correspondence,
}


def _check_keys(body: dict[str, Any], *, required: set[str], optional: set[str], what: str) -> None:
    missing = required - body.keys()
    if missing:
        raise SchemaError(f"{what} is missing field(s): {', '.join(sorted(missing))}")
    extra = body.keys() - required - optional
    if extra:
        raise SchemaError(f"{what} has unknown field(s): {', '.join(sorted(extra))}")


def parse_object(body: Any) -> StoredObject:
    """Strictly parse an object body; it must already be in normalized form."""
    canonical.check_value(body)
    if not isinstance(body, dict):
        raise SchemaError("object body must be a JSON object")
    obj_type = body.get("type")
    if obj_type not in _PARSERS:
        raise SchemaError(f"unknown object type {obj_type!r}")
    if body.get("v") != SCHEMA_VERSION or isinstance(body.get("v"), bool):
        raise SchemaError(f"unsupported schema version {body.get('v')!r} (expected {SCHEMA_VERSION})")
    obj = _PARSERS[obj_type](body)
    if obj.body() != body:
        raise SchemaError(f"{obj_type} object is not in normalized form (unsorted lists or empty optional fields)")
    return obj


def decode(data: bytes) -> StoredObject:
    return parse_object(canonical.parse_bytes(data))


def encode(obj: StoredObject) -> tuple[str, bytes]:
    data = canonical.canonical_bytes(obj.body())
    return canonical.hash_bytes(data), data


def object_type(obj: StoredObject) -> str:
    return {Node: "node", Configuration: "configuration", Commit: "commit", Correspondence: "correspondence"}[type(obj)]
