"""§11 Hashing (tests 1-3) plus object-model validation."""

import pytest

from openbom import canonical
from openbom.errors import SchemaError
from openbom.objects import decode, encode, make_configuration, make_entry, make_node, parse_object

NODE_A = "sha256:" + "a" * 64
NODE_B = "sha256:" + "b" * 64
NODE_C = "sha256:" + "c" * 64


def node(**overrides):
    fields = dict(
        org="acme",
        part_id="SRV-1U-001",
        revision="B",
        name="1U compute server",
        manufacturer="Acme",
        mpn="SRV1U",
        identifiers=[("gtin", "00012345678905")],
        uris=["https://example.com/drawings/SRV-1U-001-B.pdf"],
        attributes={"category": "server"},
    )
    fields.update(overrides)
    return make_node(**fields)


# 1
def test_hash_independent_of_key_and_unordered_list_order():
    assert canonical.hash_value({"b": 1, "a": [1, 2]}) == canonical.hash_value({"a": [1, 2], "b": 1})

    n1 = node(identifiers=[("mpn", "X"), ("gtin", "1")], uris=["https://b", "https://a"], attributes={"x": "1", "y": "2"})
    n2 = node(identifiers=[("gtin", "1"), ("mpn", "X")], uris=["https://a", "https://b", "https://a"], attributes={"y": "2", "x": "1"})
    assert encode(n1)[0] == encode(n2)[0]

    secret = b"s" * 32
    e1 = make_entry(node=NODE_B, quantity="2", refdes=["R2", "R1"])
    e2 = make_entry(node=NODE_C, quantity="1")
    c1 = make_configuration(node=NODE_A, entries=[e1, e2], secret=secret)
    c2 = make_configuration(node=NODE_A, entries=[e2, make_entry(node=NODE_B, quantity="2", refdes=["R1", "R2"])], secret=secret)
    assert encode(c1)[0] == encode(c2)[0]


# 2
@pytest.mark.parametrize(
    "build",
    [
        lambda: canonical.canonical_bytes({"a": [{"b": 1.5}]}),
        lambda: canonical.canonical_bytes(2.0),
        lambda: make_entry(node=NODE_A, quantity=2.0),
        lambda: node(attributes={"weight": 1.5}),
        lambda: parse_object({"type": "node", "v": 1.0, "kind": "part", "org": "a", "part_id": "p", "revision": "A", "name": "n"}),
        lambda: decode(b'{"kind":"part","name":"n","org":"a","part_id":"p","revision":"A","type":"node","v":1,"x":0.5}'),
    ],
)
def test_floats_rejected(build):
    with pytest.raises(SchemaError):
        build()


# 3
GOLDEN_NODE_HASH = "sha256:a31ea188b91a775c4c766f1d977e98fcc151da4405eee4a488a8c4325a7e2a10"
GOLDEN_NODE_BYTES = (
    b'{"attributes":{"category":"server"},"identifiers":[{"scheme":"gtin","value":"00012345678905"}],'
    b'"kind":"part","manufacturer":"Acme","mpn":"SRV1U","name":"1U compute server","org":"acme",'
    b'"part_id":"SRV-1U-001","revision":"B","type":"node","uris":["https://example.com/drawings/SRV-1U-001-B.pdf"],"v":1}'
)


def test_golden_vector():
    h, data = encode(node())
    assert data == GOLDEN_NODE_BYTES
    assert h == GOLDEN_NODE_HASH
    assert canonical.hash_bytes(GOLDEN_NODE_BYTES) == GOLDEN_NODE_HASH


@pytest.mark.parametrize("qty", ["0", "0.0", "-1", "01", "1.", ".5", "1e3", "two", ""])
def test_invalid_quantities(qty):
    with pytest.raises(SchemaError):
        make_entry(node=NODE_A, quantity=qty)


@pytest.mark.parametrize("qty", ["1", "2", "0.5", "10.25"])
def test_valid_quantities(qty):
    assert make_entry(node=NODE_A, quantity=qty).quantity == qty


def test_strict_parse_rejects_unsorted_lists():
    body = node(uris=["https://a", "https://b"]).body()
    body["uris"] = ["https://b", "https://a"]
    with pytest.raises(SchemaError, match="normalized"):
        parse_object(body)


def test_duplicate_entries_rejected():
    with pytest.raises(SchemaError, match="duplicate entry"):
        make_configuration(
            node=NODE_A,
            entries=[make_entry(node=NODE_B, quantity="1"), make_entry(node=NODE_B, quantity="2")],
            secret=b"s" * 32,
        )


def test_only_v1_kinds_and_rels():
    with pytest.raises(SchemaError):
        node(kind="instance")
    with pytest.raises(SchemaError):
        make_entry(node=NODE_A, quantity="1", rel="alternate")
