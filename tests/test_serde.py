from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pi_experiments.serde import canonical_json, json_text, to_primitive


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


def test_json_text_matches_standard_encoder_for_supported_values() -> None:
    value = {
        "unicode": "caf\u00e9 \"quoted\"\nline",
        "numbers": [1, -2, 3.5, float("inf"), None, True, False],
        "nested": {"empty_dict": {}, "empty_list": [], "path": Path("a/b")},
    }
    primitive = to_primitive(value)

    assert json_text(value) == json.dumps(
        primitive, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert json_text(value, indent=2) == json.dumps(
        primitive, indent=2, sort_keys=True, ensure_ascii=False
    )


def test_json_text_pretty_prints_deeply_nested_containers() -> None:
    depth = 1_200
    value: object = "leaf"
    for _ in range(depth):
        value = {"child": [value]}

    decoded = json.loads(json_text(value, indent=2))
    for _ in range(depth):
        decoded = decoded["child"][0]
    assert decoded == "leaf"


def test_canonical_json_handles_deeply_nested_containers() -> None:
    depth = 1_200
    value: object = "leaf"
    for _ in range(depth):
        value = {"child": [value]}

    decoded = json.loads(canonical_json(value))
    for _ in range(depth):
        decoded = decoded["child"][0]
    assert decoded == "leaf"
