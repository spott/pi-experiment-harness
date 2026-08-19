from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pi_experiments.serde import canonical_json, to_primitive


@dataclasses.dataclass(frozen=True)
class Example:
    path: Path
    values: tuple[int, None]


def test_to_primitive_preserves_supported_conversion_semantics() -> None:
    shared = [Example(path=Path("somewhere"), values=(1, None))]

    assert to_primitive({7: shared, "copy": shared}) == {
        "7": [{"path": "somewhere", "values": [1, None]}],
        "copy": [{"path": "somewhere", "values": [1, None]}],
    }


def test_to_primitive_rejects_circular_references() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(ValueError, match="circular reference"):
        to_primitive(value)


def test_canonical_json_handles_deeply_nested_containers() -> None:
    depth = 1_200
    value: object = "leaf"
    for _ in range(depth):
        value = {"child": [value]}

    decoded = json.loads(canonical_json(value))
    for _ in range(depth):
        decoded = decoded["child"][0]
    assert decoded == "leaf"
