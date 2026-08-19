"""Shared decoder for the OTLP ``AnyValue`` union.

OTLP encodes every attribute value, log body and nested element as an
``AnyValue``: a one-of wrapper such as ``{"stringValue": "chat"}`` or
``{"arrayValue": {"values": [...]}}``. The protobuf receiver (via
``MessageToDict``) and OTLP/JSON payloads deliver that same dict shape, so
every consumer needs identical decoding rules.

This module is deliberately dependency-free: importing only the standard
library lets ``extraction``, ``loader.otlp`` and ``api.otlp_processing`` all
use it without creating an import cycle.
"""

from __future__ import annotations

from typing import Any

ANY_VALUE_FIELDS = (
    "stringValue",
    "intValue",
    "doubleValue",
    "boolValue",
    "kvlistValue",
    "arrayValue",
    "bytesValue",
)


def decode_any_value(value_obj: dict) -> Any:
    """Recursively decode an OTLP ``AnyValue`` to a native Python value.

    Handles the full union: stringValue, intValue (OTLP sends it as a
    string), doubleValue, boolValue, kvlistValue (→ dict), arrayValue
    (→ list), bytesValue.

    ``bytesValue`` is returned unchanged. ``MessageToDict`` base64-encodes
    protobuf bytes fields and OTLP/JSON does the same, so callers already
    receive a str; decoding it here would change the value they see today.

    A value carrying none of the union fields is returned as-is.
    """
    if "stringValue" in value_obj:
        return value_obj["stringValue"]
    if "intValue" in value_obj:
        return int(value_obj["intValue"])
    if "doubleValue" in value_obj:
        return float(value_obj["doubleValue"])
    if "boolValue" in value_obj:
        return value_obj["boolValue"]
    if "kvlistValue" in value_obj:
        kv = value_obj["kvlistValue"]
        return {item.get("key", ""): decode_any_value(item.get("value", {})) for item in kv.get("values", [])}
    if "arrayValue" in value_obj:
        arr = value_obj["arrayValue"]
        return [decode_any_value(v) for v in arr.get("values", [])]
    if "bytesValue" in value_obj:
        return value_obj["bytesValue"]
    return value_obj


def is_any_value(value_obj: dict) -> bool:
    """Return True when *value_obj* carries one of the ``AnyValue`` fields."""
    for field in ANY_VALUE_FIELDS:
        if field in value_obj:
            return True
    return False


def decode_attributes(attrs_list: list[dict]) -> dict[str, Any]:
    """Decode an OTLP attributes array to a flat ``{key: value}`` dict.

    Entries whose value carries no ``AnyValue`` field are skipped, matching
    the behaviour every call site had before they shared this decoder.
    """
    result: dict[str, Any] = {}
    for attr in attrs_list:
        value_obj = attr.get("value", {})
        if is_any_value(value_obj):
            result[attr.get("key", "")] = decode_any_value(value_obj)
    return result
