"""Canonical serialization and hashing helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any


def to_primitive(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def content_hash(value: bytes | str, *, prefix: str = "sha256") -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return f"{prefix}:{hashlib.sha256(data).hexdigest()}"


def object_hash(value: Any) -> str:
    return content_hash(canonical_json(value))


def short_hash(value: Any, length: int = 12) -> str:
    return object_hash(value).split(":", 1)[1][:length]
