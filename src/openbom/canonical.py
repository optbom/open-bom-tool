"""Canonical JSON (RFC 8785 / JCS), float rejection, and hashing helpers.

Every hash is prefixed with its algorithm ("sha256:<hex>") so the algorithm can
change later (D18) without ambiguity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

import rfc8785

from .errors import SchemaError

HASH_ALGO = "sha256"
HASH_PREFIX = f"{HASH_ALGO}:"
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHORT_HASH_LEN = 12


def check_value(value: Any, path: str = "$") -> None:
    """Reject anything outside the restricted JSON value set (§3).

    Allowed: str, int, bool, None, list, and dict with str keys. Floats are
    rejected everywhere because they make canonical hashing fragile.
    """
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise SchemaError(f"floats are not allowed (found {value!r} at {path}); use a decimal string")
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            check_value(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"object keys must be strings (found {key!r} at {path})")
            check_value(item, f"{path}.{key}")
        return
    raise SchemaError(f"unsupported value type {type(value).__name__} at {path}")


def canonical_bytes(value: Any) -> bytes:
    """Serialize a value to its exact canonical bytes (JCS)."""
    check_value(value)
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        raise SchemaError(f"value cannot be canonicalized: {exc}") from exc


def hash_bytes(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def hash_value(value: Any) -> str:
    return hash_bytes(canonical_bytes(value))


def is_hash(value: Any) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def short_hash(h: str, length: int = SHORT_HASH_LEN) -> str:
    if h.startswith(HASH_PREFIX):
        return h[: len(HASH_PREFIX) + length]
    return h[:length]


def hmac_hex(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate key {key!r} in JSON object")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"non-finite number {name} is not allowed")


def parse_bytes(data: bytes) -> Any:
    """Parse stored bytes strictly (no duplicate keys, no NaN/Infinity)."""
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"stored bytes are not valid JSON: {exc}") from exc
