"""Provider-neutral metrics derived from normal Pi and harness artifacts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .base import ArtifactRecords


class GenericExtractor:
    namespace = "generic"

    def extract(
        self, artifacts: ArtifactRecords, completion: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        turns = _load_json_files(artifacts.root / "turns", numeric_only=True)
        evaluations = _load_json(artifacts.root / "evaluator.json", default=[])
        changed_files = _load_json(artifacts.root / "changed-files.json", default=[])
        usage_records = list(_usage_records(artifacts.session))
        usage_totals = _sum_usage(usage_records)
        tool_calls, tool_failures = _tool_activity(artifacts)
        compactions = _compactions(artifacts)
        patch = _read_bytes(artifacts.root / "patch.diff")
        final_tree = _read_text(artifacts.root / "final-tree.txt").strip() or None
        turn_series = [_turn_metrics(turn) for turn in turns]
        passed = [item for item in evaluations if isinstance(item, Mapping) and item.get("passed") is True]
        failed = [item for item in evaluations if isinstance(item, Mapping) and item.get("passed") is False]
        complete = dict(completion or {})
        return {
            "status": complete.get("status"),
            "exit_reason": complete.get("exit_reason"),
            "wall_seconds": complete.get("wall_seconds"),
            "steps_completed": complete.get("steps_completed"),
            "process_exit_status": complete.get("process_exit_status"),
            "turn_count": len(turns),
            "turns": turn_series,
            "provider_input_tokens_trajectory": [item["input_tokens"] for item in turn_series],
            "context_fraction_trajectory": [item["context_fraction"] for item in turn_series],
            "usage_record_count": len(usage_records),
            "usage_totals": usage_totals,
            "input_tokens": usage_totals["input_tokens"],
            "output_tokens": usage_totals["output_tokens"],
            "cache_read_tokens": usage_totals["cache_read_tokens"],
            "cache_write_tokens": usage_totals["cache_write_tokens"],
            "cost_usd": usage_totals["cost_usd"],
            "tool_call_count": sum(tool_calls.values()),
            "tool_calls_by_name": dict(sorted(tool_calls.items())),
            "tool_failure_count": tool_failures,
            "global_compaction_count": len(compactions),
            "global_compactions": compactions,
            "session_message_count": sum(item.get("type") == "message" for item in artifacts.session),
            "session_entry_count": len(artifacts.session),
            "session_serialized_bytes": sum(len(line) + 1 for line in artifacts.session_lines),
            "session_parse_errors": artifacts.session_parse_errors,
            "event_count": len(artifacts.events),
            "event_parse_errors": artifacts.event_parse_errors,
            "changed_file_count": len(changed_files) if isinstance(changed_files, list) else None,
            "lines_added": _patch_lines(patch)[0],
            "lines_deleted": _patch_lines(patch)[1],
            "patch_size_bytes": len(patch),
            "binary_change_count": patch.count(b"GIT binary patch") + patch.count(b"Binary files "),
            "final_tree_sha": final_tree,
            "evaluation_count": len(evaluations) if isinstance(evaluations, list) else 0,
            "evaluations_passed": len(passed),
            "evaluations_failed": len(failed),
        }


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _load_json_files(directory: Path, *, numeric_only: bool = False) -> list[Mapping[str, Any]]:
    if not directory.is_dir():
        return []
    result: list[Mapping[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if numeric_only and not path.stem.isdigit():
            continue
        value = _load_json(path, default={})
        if isinstance(value, Mapping):
            result.append(value)
    return result


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _usage_records(entries: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    for entry in entries:
        for value in _walk(entry):
            usage = value.get("usage")
            if isinstance(usage, Mapping) and _looks_like_usage(usage):
                yield usage


def _looks_like_usage(value: Mapping[str, Any]) -> bool:
    keys = {str(key).replace("_", "").lower() for key in value}
    return bool(keys & {"input", "inputtokens", "output", "outputtokens", "cacheread", "cachewrite", "cost"})


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usage_value(usage: Mapping[str, Any], *keys: str) -> float | int | None:
    for key in keys:
        value = _number(usage.get(key))
        if value is not None:
            return value
    tokens = usage.get("tokens")
    if isinstance(tokens, Mapping):
        for key in keys:
            value = _number(tokens.get(key))
            if value is not None:
                return value
    return None


def _cost(usage: Mapping[str, Any]) -> float | int | None:
    value = usage.get("cost")
    if isinstance(value, Mapping):
        return _number(value.get("total"))
    return _number(value)


def _sum_usage(records: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    aliases = {
        "input_tokens": ("input", "inputTokens", "input_tokens"),
        "output_tokens": ("output", "outputTokens", "output_tokens"),
        "cache_read_tokens": ("cacheRead", "cache_read", "cacheReadTokens"),
        "cache_write_tokens": ("cacheWrite", "cache_write", "cacheWriteTokens"),
    }
    totals: dict[str, float | int | None] = {}
    values = list(records)
    for name, keys in aliases.items():
        observed = [_usage_value(record, *keys) for record in values]
        present = [value for value in observed if value is not None]
        totals[name] = sum(present) if present else None
    costs = [_cost(record) for record in values]
    present_costs = [value for value in costs if value is not None]
    totals["cost_usd"] = sum(present_costs) if present_costs else None
    return totals


def _turn_metrics(turn: Mapping[str, Any]) -> dict[str, Any]:
    usage = turn.get("usage") if isinstance(turn.get("usage"), Mapping) else {}
    context = usage.get("contextUsage") if isinstance(usage.get("contextUsage"), Mapping) else {}
    percent = _number(context.get("percent"))
    fraction = percent / 100 if percent is not None else None
    return {
        "turn_index": turn.get("turn_index"),
        "scenario_step": turn.get("scenario_step"),
        "elapsed_seconds": turn.get("elapsed_seconds"),
        "input_tokens": _usage_value(usage, "input", "inputTokens", "input_tokens"),
        "output_tokens": _usage_value(usage, "output", "outputTokens", "output_tokens"),
        "cache_read_tokens": _usage_value(usage, "cacheRead", "cache_read", "cacheReadTokens"),
        "cache_write_tokens": _usage_value(usage, "cacheWrite", "cache_write", "cacheWriteTokens"),
        "cost_usd": _cost(usage),
        "context_tokens": _number(context.get("tokens")),
        "context_window": _number(context.get("contextWindow")),
        "context_fraction": fraction,
    }


def _tool_activity(artifacts: ArtifactRecords) -> tuple[Counter[str], int]:
    calls: dict[str, str] = {}
    failures: set[str] = set()
    for entry in artifacts.session:
        for value in _walk(entry):
            kind = str(value.get("type", "")).replace("_", "").lower()
            if kind in {"toolcall", "tooluse"}:
                name = value.get("name", value.get("toolName"))
                call_id = value.get("id", value.get("toolCallId"))
                if isinstance(name, str):
                    calls[str(call_id) if call_id is not None else f"anonymous-{len(calls)}"] = name
            role = str(value.get("role", "")).replace("_", "").lower()
            if kind in {"toolresult", "tooloutput"} or role == "toolresult":
                error = value.get("error")
                if value.get("isError") is True or error is not None and error is not False and error != "":
                    call_id = value.get("toolCallId", value.get("id", f"failure-{len(failures)}"))
                    failures.add(str(call_id))
    if not calls:
        for index, event in enumerate(artifacts.events):
            if event.get("type") in {"tool_execution_start", "tool_start"}:
                name = event.get("toolName", event.get("tool"))
                if isinstance(name, str):
                    calls[str(event.get("toolCallId", index))] = name
            if event.get("type") in {"tool_execution_end", "tool_end"} and event.get("isError") is True:
                failures.add(str(event.get("toolCallId", index)))
    return Counter(calls.values()), len(failures)


def _compactions(artifacts: ArtifactRecords) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    open_indices: list[int] = []
    for index, event in enumerate(artifacts.events):
        if event.get("type") == "compaction_start":
            result.append({"event_index": index, "reason": event.get("reason"), "completed": False})
            open_indices.append(len(result) - 1)
        elif event.get("type") == "compaction_end" and open_indices:
            target = open_indices.pop(0)
            result[target]["completed"] = True
            result[target]["end_event_index"] = index
    if result:
        return result
    for index, entry in enumerate(artifacts.session):
        if str(entry.get("type", "")).lower() in {"compaction", "compaction_summary"}:
            result.append({"session_entry_index": index, "reason": entry.get("reason"), "completed": True})
    return result


def _patch_lines(patch: bytes) -> tuple[int, int]:
    added = deleted = 0
    for line in patch.splitlines():
        if line.startswith(b"+") and not line.startswith(b"+++"):
            added += 1
        elif line.startswith(b"-") and not line.startswith(b"---"):
            deleted += 1
    return added, deleted
