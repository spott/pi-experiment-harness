"""Canonical serialization and hashing helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any


def to_primitive(value: Any) -> Any:
    """Convert supported values without recursing through nested containers."""
    result: list[Any] = [None]
    active_container_ids: set[int] = set()
    stack: list[tuple[str, Any, list[Any] | dict[str, Any] | None, int | str | None]] = [
        ("visit", value, result, 0)
    ]

    while stack:
        action, current, parent, slot = stack.pop()
        if action == "leave":
            active_container_ids.remove(current)
            continue

        if isinstance(current, Path):
            converted: Any = str(current)
            children: list[tuple[int | str, Any]] | None = None
        elif isinstance(current, (str, int, float, bool)) or current is None:
            converted = current
            children = None
        elif dataclasses.is_dataclass(current):
            converted = {}
            children = [
                (field.name, getattr(current, field.name))
                for field in dataclasses.fields(current)
            ]
        elif isinstance(current, dict):
            converted = {}
            children = [(str(key), item) for key, item in current.items()]
        elif isinstance(current, (tuple, list)):
            converted = [None] * len(current)
            children = list(enumerate(current))
        else:
            raise TypeError(f"cannot serialize {type(current).__name__}")

        assert parent is not None and slot is not None
        parent[slot] = converted
        if children is None:
            continue

        container_id = id(current)
        if container_id in active_container_ids:
            raise ValueError("circular reference detected")
        active_container_ids.add(container_id)
        stack.append(("leave", container_id, None, None))
        stack.extend(
            ("visit", child, converted, child_slot)
            for child_slot, child in reversed(children)
        )

    return result[0]


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
