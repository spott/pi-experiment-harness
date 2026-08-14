"""Metric extractor interfaces and artifact loading helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ArtifactRecords:
    root: Path
    events: tuple[Mapping[str, Any], ...]
    session: tuple[Mapping[str, Any], ...]
    event_parse_errors: int
    session_parse_errors: int
    session_lines: tuple[bytes, ...]


class MetricExtractor(Protocol):
    namespace: str

    def extract(
        self, artifacts: ArtifactRecords, completion: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]: ...


def load_artifacts(root: str | Path) -> ArtifactRecords:
    directory = Path(root).resolve()
    events, event_errors, _ = _load_jsonl(directory / "events.jsonl")
    session, session_errors, session_lines = _load_jsonl(directory / "session.jsonl")
    return ArtifactRecords(
        root=directory,
        events=tuple(events),
        session=tuple(session),
        event_parse_errors=event_errors,
        session_parse_errors=session_errors,
        session_lines=tuple(session_lines),
    )


def _load_jsonl(
    path: Path,
) -> tuple[list[Mapping[str, Any]], int, list[bytes]]:
    if not path.is_file():
        return [], 0, []
    records: list[Mapping[str, Any]] = []
    errors = 0
    lines = path.read_bytes().splitlines()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            errors += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            errors += 1
    return records, errors, lines
