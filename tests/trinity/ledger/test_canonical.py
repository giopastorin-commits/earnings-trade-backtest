import math

import pytest

from trinity.ledger import (
    CanonicalizationError,
    UnsupportedCanonicalValue,
    canonicalize_json,
    canonicalize_json_document,
    canonicalize_opaque,
)


def test_object_insertion_order_is_irrelevant():
    assert canonicalize_json({"b": 2, "a": 1}) == canonicalize_json({"a": 1, "b": 2})
    assert canonicalize_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_nested_objects_are_deterministic():
    left = {"z": {"b": True, "a": None}, "a": [{"y": 2, "x": 1}]}
    right = {"a": [{"x": 1, "y": 2}], "z": {"a": None, "b": True}}
    assert canonicalize_json(left) == canonicalize_json(right)


def test_arrays_preserve_semantic_order():
    assert canonicalize_json([1, 2, 3]) != canonicalize_json([3, 2, 1])


def test_utf8_round_trip_and_jcs_utf16_key_order():
    # U+10000 sorts before U+E000 by UTF-16 code units (JCS), unlike code-point order.
    value = {"\ue000": "caffè", "\U00010000": "東京"}
    encoded = canonicalize_json(value)
    assert encoded.decode("utf-8") == '{"𐀀":"東京","":"caffè"}'


def test_decimal_strings_remain_exact_strings():
    assert canonicalize_json({"price": "1234567890.0000000001"}) == (
        b'{"price":"1234567890.0000000001"}'
    )


@pytest.mark.parametrize("value", [-0.0, 0.0])
def test_signed_zero_is_normalized(value):
    assert canonicalize_json(value) == b"0"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(UnsupportedCanonicalValue):
        canonicalize_json(value)


def test_nonzero_binary_float_is_rejected():
    with pytest.raises(UnsupportedCanonicalValue, match="decimal string"):
        canonicalize_json(0.1)


def test_opaque_bytes_round_trip_exactly():
    payload = b"\x00\xff  bytes\r\n"
    assert canonicalize_opaque(payload) == payload


def test_structured_whitespace_is_irrelevant():
    compact = canonicalize_json_document(b'{"a":1,"b":[true,null]}')
    spaced = canonicalize_json_document(' { "b" : [ true, null ], "a" : 1 } ')
    assert compact == spaced == b'{"a":1,"b":[true,null]}'


def test_structured_duplicate_keys_are_rejected():
    with pytest.raises(CanonicalizationError, match="duplicate object key"):
        canonicalize_json_document('{"a":1,"a":2}')


def test_float_in_json_document_is_rejected_but_negative_zero_normalizes():
    with pytest.raises(UnsupportedCanonicalValue):
        canonicalize_json_document('{"value":1.25}')
    assert canonicalize_json_document('{"value":-0.0}') == b'{"value":0}'
