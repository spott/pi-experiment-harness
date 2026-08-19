"""Metrics for the versioned task-compaction extension marker schema."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..serde import canonical_json
from .base import ArtifactRecords


@dataclass(frozen=True)
class _Marker:
    event: str
    task_id: str
    entry_index: int
    value: Mapping[str, Any]


class TaskCompactionExtractor:
    namespace = "task_compaction"

    def extract(
        self, artifacts: ArtifactRecords, completion: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        markers = _markers(artifacts.session)
        begins = [marker for marker in markers if marker.event == "begin"]
        ends = [marker for marker in markers if marker.event == "end"]
        pairs, open_begins, unmatched_ends = _pair_markers(begins, ends)
        diagnostics = _diagnostics((*artifacts.session, *artifacts.events))
        explicit_accepts = {item[0] for item in diagnostics if item[1] is True}
        rejections = [(item[0], item[2] or "unspecified") for item in diagnostics if item[1] is False]
        rejection_reasons = Counter(reason for _, reason in rejections)
        compaction_indices = [
            index
            for index, entry in enumerate(artifacts.session)
            if str(entry.get("type", "")).lower() in {"compaction", "compaction_summary"}
        ]
        regions = [
            _region_metrics(begin, end, artifacts, compaction_indices)
            for begin, end in pairs
        ]
        ratios = [item["compression_ratio"] for item in regions if item["compression_ratio"] is not None]
        expanded_ids = _expanded_task_ids(artifacts.session)
        return {
            "schema_version": 1,
            "begin_task_count": len(begins),
            "end_task_count": len(ends),
            "closed_task_count": len(pairs),
            "open_task_count": len(open_begins),
            "abandoned_task_count": len(open_begins),
            "unmatched_end_count": len(unmatched_ends),
            "accepted_region_count": len(explicit_accepts) if diagnostics else None,
            "rejected_region_count": len(rejections) if diagnostics else None,
            "acceptance_diagnostics_available": bool(diagnostics),
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            "raw_region_serialized_bytes": sum(item["raw_serialized_bytes"] for item in regions),
            "summary_serialized_bytes": sum(item["summary_serialized_bytes"] for item in regions),
            "compression_ratios": ratios,
            "median_compression_ratio": _median(ratios),
            "regions": regions,
            "expand_task_count": len(expanded_ids),
            "expanded_task_ids": expanded_ids,
            "global_compactions_after_task_regions": sum(
                item["global_compactions_after_region"] for item in regions
            ),
            "raw_task_bodies_reintroduced_after_global_compaction": None,
            "reintroduction_measurement": "unavailable without provider-request projection diagnostics",
        }


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _normalized_key(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _markers(entries: tuple[Mapping[str, Any], ...]) -> list[_Marker]:
    result: list[_Marker] = []
    seen: set[tuple[int, str, str, str]] = set()
    for index, entry in enumerate(entries):
        for value in _walk(entry):
            event = _normalized_key(value, "event", "markerEvent", "marker_event")
            version = _normalized_key(value, "schemaVersion", "schema_version")
            task_id = _normalized_key(value, "taskId", "task_id")
            if (
                not isinstance(event, str)
                or event not in {"begin", "end"}
                or version != 1
                or not isinstance(task_id, str)
            ):
                continue
            signature = (index, event, task_id, canonical_json(value))
            if signature not in seen:
                result.append(_Marker(event, task_id, index, value))
                seen.add(signature)
    return result


def _pair_markers(
    begins: list[_Marker], ends: list[_Marker]
) -> tuple[list[tuple[_Marker, _Marker]], list[_Marker], list[_Marker]]:
    available = list(sorted(ends, key=lambda item: item.entry_index))
    pairs: list[tuple[_Marker, _Marker]] = []
    open_begins: list[_Marker] = []
    for begin in sorted(begins, key=lambda item: item.entry_index):
        match = next(
            (
                end
                for end in available
                if end.task_id == begin.task_id and end.entry_index >= begin.entry_index
            ),
            None,
        )
        if match is None:
            open_begins.append(begin)
        else:
            pairs.append((begin, match))
            available.remove(match)
    return pairs, open_begins, available


def _summary(marker: _Marker) -> Mapping[str, Any]:
    value = marker.value
    for key in ("summary", "taskSummary", "task_summary"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    fields = {
        key: value[key]
        for key in (
            "objective",
            "outcome",
            "attempted",
            "learnings",
            "decisions",
            "files_read",
            "files_modified",
            "artifacts",
            "verification",
            "open_threads",
        )
        if key in value
    }
    return fields


def _region_metrics(
    begin: _Marker,
    end: _Marker,
    artifacts: ArtifactRecords,
    compaction_indices: list[int],
) -> dict[str, Any]:
    first = max(0, begin.entry_index)
    last = min(len(artifacts.session_lines) - 1, end.entry_index)
    raw_bytes = sum(len(line) + 1 for line in artifacts.session_lines[first : last + 1])
    summary = _summary(end)
    summary_bytes = len(canonical_json(summary).encode("utf-8")) if summary else 0
    calls = 0
    model_turns = 0
    for entry in artifacts.session[first : last + 1]:
        if entry.get("type") == "message":
            message = entry.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                model_turns += 1
        calls += sum(
            str(value.get("type", "")).replace("_", "").lower() in {"toolcall", "tooluse"}
            for value in _walk(entry)
        )
    return {
        "task_id": begin.task_id,
        "begin_entry_index": begin.entry_index,
        "end_entry_index": end.entry_index,
        "entry_span": end.entry_index - begin.entry_index + 1,
        "model_turns": model_turns,
        "tool_calls": calls,
        "raw_serialized_bytes": raw_bytes,
        "summary_serialized_bytes": summary_bytes,
        "compression_ratio": raw_bytes / summary_bytes if summary_bytes else None,
        "global_compactions_after_region": sum(index > end.entry_index for index in compaction_indices),
    }


def _diagnostics(
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[str, bool, str | None]]:
    result: list[tuple[str, bool, str | None]] = []
    seen: set[tuple[str, bool, str | None]] = set()
    for record in records:
        for value in _walk(record):
            task_id = _normalized_key(value, "taskId", "task_id")
            accepted = _normalized_key(value, "accepted", "regionAccepted", "region_accepted")
            reason = _normalized_key(value, "rejectionReason", "rejection_reason", "reason")
            if isinstance(task_id, str) and isinstance(accepted, bool):
                item = (task_id, accepted, str(reason) if reason is not None else None)
                if item not in seen:
                    result.append(item)
                    seen.add(item)
    return result


def _expanded_task_ids(entries: tuple[Mapping[str, Any], ...]) -> list[str]:
    result: list[str] = []
    seen_calls: set[str] = set()
    for entry in entries:
        for value in _walk(entry):
            kind = str(value.get("type", "")).replace("_", "").lower()
            name = value.get("name", value.get("toolName"))
            if kind not in {"toolcall", "tooluse"} or name != "expand_task":
                continue
            call_id = str(value.get("id", value.get("toolCallId", len(seen_calls))))
            if call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            arguments = value.get("arguments", value.get("input", {}))
            task_id = None
            if isinstance(arguments, Mapping):
                task_id = _normalized_key(arguments, "task_id", "taskId")
            result.append(str(task_id) if task_id is not None else "<unknown>")
    return result


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
