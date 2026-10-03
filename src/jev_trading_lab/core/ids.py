"""Stable SHA-256 identities over a deliberately small canonical JSON domain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

type CanonicalValue = (
    None | bool | int | str | list[CanonicalValue] | dict[str, CanonicalValue]
)


def _canonical_value(value: object) -> CanonicalValue:
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("canonical JSON integers must fit signed 64-bit range")
        return value
    if value is None or type(value) in {bool, str}:
        return value  # type: ignore[return-value]
    if type(value) is list:
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, CanonicalValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical JSON mapping keys must be strings")
            normalized[key] = _canonical_value(item)
        return normalized
    raise TypeError(
        "canonical JSON values must be null, bool, built-in int, str, list, or mapping"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Encode supported values as sorted, compact, deterministic UTF-8 JSON."""
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(namespace: str, payload: object) -> str:
    """Return a namespace-separated canonical SHA-256 hexadecimal identity."""
    if type(namespace) is not str or not namespace:
        raise ValueError("canonical hash namespace must be a non-empty string")
    envelope = {"namespace": namespace, "payload": _canonical_value(payload)}
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
