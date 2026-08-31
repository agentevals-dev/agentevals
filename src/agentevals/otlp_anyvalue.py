"""Shared decoder for the OTLP ``AnyValue`` union.

OTLP encodes every attribute value, log body and nested element as an
``AnyValue``: a one-of wrapper such as ``{"stringValue": "chat"}`` or
``{"arrayValue": {"values": [...]}}``. The protobuf receiver (via
``MessageToDict``) and OTLP/JSON payloads deliver that same dict shape, so
every consumer needs identical decoding rules.

This module depends only on the standard library and ``trace_attrs`` (a leaf
constants module), so ``extraction``, ``loader.otlp`` and ``api.otlp_processing``
can all use it without creating an import cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from .trace_attrs import SPEC_CONTAINER_ATTRS

logger = logging.getLogger(__name__)

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


def decode_attribute(key: str, value_obj: dict) -> tuple[bool, Any]:
    """Decode one attribute, applying the container allowlist.

    Returns ``(keep, value)``. Scalars are always kept. A list or dict is kept
    only when *key* is in :data:`SPEC_CONTAINER_ATTRS`; otherwise it is dropped,
    which is what ``extraction.py`` did with containers before this decoder was
    shared.

    Dropping rather than serialising is deliberate: JSON-dumping the value would
    put a blob back into user-visible output, which is the symptom #173 is
    about. What the default *should* be is tracked in #208.
    """
    value = decode_any_value(value_obj)
    if isinstance(value, (list, dict)) and key not in SPEC_CONTAINER_ATTRS:
        logger.warning(
            "Dropping container value for %s (got %s); only spec container attributes are kept",
            key,
            type(value).__name__,
        )
        return False, None
    return True, value


def decode_attributes(attrs_list: list[dict]) -> dict[str, Any]:
    """Decode an OTLP attributes array to a flat ``{key: value}`` dict.

    Entries whose value carries no ``AnyValue`` field are skipped, matching the
    behaviour every call site had before they shared this decoder.

    Container values survive only for the keys in
    :data:`~agentevals.trace_attrs.SPEC_CONTAINER_ATTRS`. Keeping the allowlist
    here rather than narrowing per consumer means an attribute nobody has
    thought about cannot become an unhashable dict key downstream - there is
    nothing to remember, because it was never widened in the first place.
    """
    result: dict[str, Any] = {}
    for attr in attrs_list:
        value_obj = attr.get("value", {})
        if not is_any_value(value_obj):
            continue
        key = attr.get("key", "")
        keep, value = decode_attribute(key, value_obj)
        if keep:
            result[key] = value
    return result
