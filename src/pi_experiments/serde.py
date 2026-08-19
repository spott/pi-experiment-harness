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


def json_text(value: Any, *, indent: int | None = None) -> str:
    """Encode supported values as JSON without recursive container traversal."""
    primitive = to_primitive(value)
    pieces: list[str] = []
    stack: list[tuple[str, Any, int]] = [("value", primitive, 0)]

    while stack:
        action, current, depth = stack.pop()
        if action == "raw":
            pieces.append(current)
            continue

        if isinstance(current, (str, int, float, bool)) or current is None:
            pieces.append(json.dumps(current, ensure_ascii=False))
            continue

        if isinstance(current, list):
            if not current:
                pieces.append("[]")
                continue
            child_depth = depth + 1
            padding = "" if indent is None else "\n" + " " * (indent * child_depth)
            separator = "," if indent is None else "," + padding
            closing = "]" if indent is None else "\n" + " " * (indent * depth) + "]"
            pieces.append("[" + padding)
            stack.append(("raw", closing, depth))
            for index in range(len(current) - 1, -1, -1):
                if index < len(current) - 1:
                    stack.append(("raw", separator, depth))
                stack.append(("value", current[index], child_depth))
            continue

        if isinstance(current, dict):
            if not current:
                pieces.append("{}")
                continue
            child_depth = depth + 1
            padding = "" if indent is None else "\n" + " " * (indent * child_depth)
            separator = "," if indent is None else "," + padding
            key_separator = ":" if indent is None else ": "
            closing = "}" if indent is None else "\n" + " " * (indent * depth) + "}"
            items = sorted(current.items())
            pieces.append("{" + padding)
            stack.append(("raw", closing, depth))
            for index in range(len(items) - 1, -1, -1):
                key, item = items[index]
                if index < len(items) - 1:
                    stack.append(("raw", separator, depth))
                stack.append(("value", item, child_depth))
                encoded_key = json.dumps(key, ensure_ascii=False)
                stack.append(("raw", encoded_key + key_separator, depth))
            continue

        raise TypeError(f"cannot serialize {type(current).__name__}")

    return "".join(pieces)


def canonical_json(value: Any) -> str:
    return json_text(value)


def content_hash(value: bytes | str, *, prefix: str = "sha256") -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return f"{prefix}:{hashlib.sha256(data).hexdigest()}"


def object_hash(value: Any) -> str:
    return content_hash(canonical_json(value))


def short_hash(value: Any, length: int = 12) -> str:
    return object_hash(value).split(":", 1)[1][:length]
