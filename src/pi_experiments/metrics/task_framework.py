"""Metrics for the v2 semantic task framework and its captured worker runs."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..serde import canonical_json
from .base import ArtifactRecords

_TASK_EVENT = "pi-task-framework/task-event"
_EVALUATION = "pi-task-framework/evaluation"


@dataclass(frozen=True)
class _Session:
    session_id: str
    entries: tuple[Mapping[str, Any], ...]
    worker_id: str | None = None


class TaskFrameworkExtractor:
    namespace = "task_framework"

    def extract(
        self, artifacts: ArtifactRecords, completion: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        sessions, routes, bootstraps, artifact_errors = _load_sessions(artifacts)
        events = [
            (session, event)
            for session in sessions
            for event in _custom_records(session.entries, _TASK_EVENT, nested="event")
        ]
        tasks = _tasks(events)
        task_rows = _task_rows(tasks)
        tool_calls = [call for session in sessions for call in _tool_calls(session)]
        projections = [
            record
            for session in sessions
            for record in _custom_records(session.entries, _EVALUATION)
            if record.get("kind") == "context_projection"
        ]
        global_compactions = [
            record
            for session in sessions
            for record in _custom_records(session.entries, _EVALUATION)
            if record.get("kind") == "global_compaction"
        ]
        projection_rejections = Counter(
            str(reason)
            for record in (*projections, *global_compactions)
            for rejection in _mapping_list(record.get("projectionRejections"))
            for reason in rejection.get("reasons", [])
            if isinstance(reason, str)
        )
        outputs = [event for _, event in events if event.get("type") == "output_preserved"]
        protected = [event for _, event in events if event.get("type") == "user_response_protected"]
        projection_metrics = [
            record["metrics"] for record in projections if isinstance(record.get("metrics"), Mapping)
        ]
        local_depths = [row["local_depth"] for row in task_rows]
        semantic_depths = [row["semantic_depth"] for row in task_rows if row["semantic_depth"] is not None]
        agent_depths = [row["agent_depth"] for row in task_rows if row["agent_depth"] is not None]
        durations = [row["duration_seconds"] for row in task_rows if row["duration_seconds"] is not None]
        summary_sizes = [row["summary_size_bytes"] for row in task_rows if row["summary_size_bytes"] is not None]
        worker = _worker_metrics(routes, bootstraps, tasks, sessions, completion)
        context = _configured_features(artifacts.root)
        inspect_calls = [call for call in tool_calls if call["name"] == "inspect_task"]
        read_calls = [call for call in tool_calls if call["name"] == "read_preserved_output"]
        join_waits = _join_waits(sessions)
        replay_counts = [_integer(item.get("replayedMessageCount")) or 0 for item in projection_metrics]
        cascade_depths = [_integer(item.get("maxReplayCascadeDepth")) or 0 for item in projection_metrics]
        pin_tokens = [_number(item.get("pinnedClosureEstimatedTokens")) or 0 for item in projection_metrics]
        raw_tokens = [_number(item.get("inputEstimatedTokens")) for item in projection_metrics]
        projected_tokens = [_number(item.get("outputEstimatedTokens")) for item in projection_metrics]
        raw_bytes = [_number(item.get("inputSerializedBytes")) for item in projection_metrics]
        projected_bytes = [_number(item.get("outputSerializedBytes")) for item in projection_metrics]
        raw_messages = [_number(item.get("inputMessageCount")) for item in projection_metrics]
        projected_messages = [_number(item.get("outputMessageCount")) for item in projection_metrics]
        return {
            "schema_version": 2,
            "artifact_parse_errors": artifact_errors,
            "configured_features": context,
            "summary_arm_meaning": (
                "retention-level: summary authorship may still occur when summaries=false; "
                "the authored closure is scrubbed and not durably retained"
            ),
            "session_count": len(sessions),
            "worker_session_count": sum(session.worker_id is not None for session in sessions),
            "task_count": len(task_rows),
            "task_status_counts": dict(sorted(Counter(row["status"] for row in task_rows).items())),
            "tasks": task_rows,
            "local_depth_distribution": _distribution(local_depths),
            "semantic_depth_distribution": _distribution(semantic_depths),
            "agent_depth_distribution": _distribution(agent_depths),
            "child_count_distribution": _distribution([row["child_count"] for row in task_rows]),
            "task_duration_seconds": durations,
            "median_task_duration_seconds": _median(durations),
            "summary_size_bytes": summary_sizes,
            "total_summary_size_bytes": sum(summary_sizes),
            "preserved_output_count": len(outputs),
            "pinned_output_count": sum(bool(_output(event).get("pin")) for event in outputs),
            "protected_interaction_count": len(protected),
            "inspect_task_count": len(inspect_calls),
            "preserved_output_read_count": len(read_calls),
            "projection_count": len(projections),
            "raw_context_estimated_tokens": raw_tokens,
            "projected_context_estimated_tokens": projected_tokens,
            "raw_context_serialized_bytes": raw_bytes,
            "projected_context_serialized_bytes": projected_bytes,
            "raw_context_message_counts": raw_messages,
            "projected_context_message_counts": projected_messages,
            "latest_raw_context_estimated_tokens": raw_tokens[-1] if raw_tokens else None,
            "latest_projected_context_estimated_tokens": projected_tokens[-1] if projected_tokens else None,
            "latest_context_compression_ratio": _ratio(
                projected_tokens[-1] if projected_tokens else None,
                raw_tokens[-1] if raw_tokens else None,
            ),
            "pin_retained_closure_estimated_tokens": pin_tokens,
            "max_pin_retained_closure_estimated_tokens": max(pin_tokens, default=0),
            "replay_projection_count": sum(value > 0 for value in replay_counts),
            "replayed_message_occurrences": sum(replay_counts),
            "max_replay_cascade_depth": max(cascade_depths, default=0),
            "projection_rejection_reasons": dict(sorted(projection_rejections.items())),
            "global_compaction_decision_count": len(global_compactions),
            "global_compaction_cancelled_count": sum(
                record.get("cancelled") is True for record in global_compactions
            ),
            "global_compaction_alignment_counts": dict(
                sorted(
                    Counter(
                        str(record.get("alignment", "cancelled"))
                        for record in global_compactions
                    ).items()
                )
            ),
            "join_wait_seconds": join_waits,
            "median_join_wait_seconds": _median(join_waits),
            "spawn_task_count": sum(call["name"] == "spawn_task" for call in tool_calls),
            "poll_task_count": sum(call["name"] == "poll_task" for call in tool_calls),
            "join_tasks_count": sum(call["name"] == "join_tasks" for call in tool_calls),
            **worker,
        }


def _load_sessions(
    artifacts: ArtifactRecords,
) -> tuple[list[_Session], list[Mapping[str, Any]], list[Mapping[str, Any]], int]:
    sessions = [_Session(_session_id(artifacts.session, "coordinator"), artifacts.session)]
    routes: list[Mapping[str, Any]] = []
    bootstraps: list[Mapping[str, Any]] = []
    errors = 0
    root = artifacts.root / "task-framework-runs"
    if not root.is_dir():
        return sessions, routes, bootstraps, errors
    for run in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted((run / "workers").glob("*.json")):
            value = _load_json(path)
            if isinstance(value, Mapping):
                routes.append(value)
            else:
                errors += 1
        for path in sorted((run / "bootstraps").glob("*.json")):
            value = _load_json(path)
            if isinstance(value, Mapping):
                bootstraps.append(value)
            else:
                errors += 1
        for path in sorted((run / "sessions").glob("*.jsonl")):
            entries, parse_errors = _load_jsonl(path)
            errors += parse_errors
            sessions.append(_Session(_session_id(entries, path.stem), tuple(entries), path.stem))
    return sessions, routes, bootstraps, errors


def _custom_records(
    entries: Iterable[Mapping[str, Any]], custom_type: str, *, nested: str | None = None
) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for entry in entries:
        if entry.get("type") != "custom" or entry.get("customType") != custom_type:
            continue
        data = entry.get("data")
        if not isinstance(data, Mapping) or data.get("schemaVersion") != 1:
            continue
        value = data.get(nested) if nested else data
        if isinstance(value, Mapping):
            records.append(value)
    return records


def _tasks(
    events: list[tuple[_Session, Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for session, event in events:
        task_id = event.get("taskId")
        event_type = event.get("type")
        if not isinstance(task_id, str):
            continue
        if event_type == "task_created" and task_id not in tasks:
            execution = event.get("execution") if isinstance(event.get("execution"), Mapping) else {}
            tasks[task_id] = {
                "task_id": task_id,
                "parent_id": event.get("parentTaskId") if isinstance(event.get("parentTaskId"), str) else None,
                "local_depth": _integer(event.get("localDepth")) or 0,
                "created_at": _number(event.get("at")),
                "started_at": None,
                "completed_at": None,
                "status": "open",
                "summary": None,
                "session_id": session.session_id,
                "execution": execution,
            }
        elif task_id in tasks and event_type == "task_started":
            tasks[task_id]["started_at"] = _number(event.get("at"))
        elif task_id in tasks and event_type in {
            "task_completed",
            "task_failed",
            "task_cancelled",
        }:
            tasks[task_id]["completed_at"] = _number(event.get("at"))
            tasks[task_id]["status"] = str(event_type).removeprefix("task_")
            if isinstance(event.get("summary"), Mapping):
                tasks[task_id]["summary"] = event["summary"]
    return tasks


def _task_rows(tasks: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    children = Counter(
        str(task["parent_id"])
        for task in tasks.values()
        if isinstance(task.get("parent_id"), str)
    )
    semantic_depths: dict[str, int | None] = {}

    def semantic_depth(task_id: str, seen: set[str] | None = None) -> int | None:
        if task_id in semantic_depths:
            return semantic_depths[task_id]
        chain = set(seen or ())
        if task_id in chain:
            semantic_depths[task_id] = None
            return None
        chain.add(task_id)
        task = tasks[task_id]
        parent_id = task.get("parent_id")
        if parent_id is None:
            value: int | None = 1
        elif parent_id not in tasks:
            value = None
        else:
            parent_depth = semantic_depth(str(parent_id), chain)
            value = parent_depth + 1 if parent_depth is not None else None
        semantic_depths[task_id] = value
        return value

    session_agent_depth: dict[str, int] = {}
    for task in tasks.values():
        execution = task.get("execution")
        if isinstance(execution, Mapping) and execution.get("kind") == "worker":
            depth = _integer(execution.get("agentDepth"))
            if depth is not None:
                session_agent_depth[str(task["session_id"])] = depth
    rows: list[dict[str, Any]] = []
    for task_id in sorted(tasks, key=lambda key: (_number(tasks[key].get("created_at")) or 0, key)):
        task = tasks[task_id]
        created = _number(task.get("created_at"))
        completed = _number(task.get("completed_at"))
        summary = task.get("summary")
        execution = task.get("execution") if isinstance(task.get("execution"), Mapping) else {}
        agent_depth = (
            _integer(execution.get("agentDepth"))
            if execution.get("kind") == "worker"
            else session_agent_depth.get(str(task["session_id"]), 0)
        )
        rows.append(
            {
                "task_id": task_id,
                "parent_id": task.get("parent_id"),
                "status": task["status"],
                "local_depth": task["local_depth"],
                "semantic_depth": semantic_depth(task_id),
                "agent_depth": agent_depth,
                "child_count": children[task_id],
                "duration_seconds": (
                    max(0.0, (completed - created) / 1000)
                    if created is not None and completed is not None
                    else None
                ),
                "summary_size_bytes": (
                    len(canonical_json(summary).encode("utf-8"))
                    if isinstance(summary, Mapping)
                    else None
                ),
                "execution_kind": execution.get("kind"),
                "session_id": task["session_id"],
            }
        )
    return rows


def _worker_metrics(
    routes: list[Mapping[str, Any]],
    bootstraps: list[Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
    sessions: list[_Session],
    completion: Mapping[str, Any] | None,
) -> dict[str, Any]:
    route_counts = Counter(str(route.get("status", "unknown")) for route in routes)
    startup: list[float] = []
    intervals: list[tuple[float, float]] = []
    semantic_failures = derived_failures = cancellations = successes = 0
    for route in routes:
        task = tasks.get(str(route.get("taskId")))
        created = _number(task.get("created_at")) if task else None
        started = _number(route.get("startedAt"))
        exited = _number(route.get("exitedAt"))
        if created is not None and started is not None:
            startup.append(max(0.0, (started - created) / 1000))
        if started is not None and exited is not None:
            intervals.append((started / 1000, max(started, exited) / 1000))
        semantic = task.get("status") if task else "open"
        route_status = route.get("status")
        if semantic == "failed":
            semantic_failures += 1
        elif semantic == "cancelled" or route_status == "cancelled":
            cancellations += 1
        elif not (
            semantic == "completed"
            and route_status == "completed"
            and route.get("exitCode") == 0
        ) and route_status in {"failed", "completed"}:
            derived_failures += 1
        elif semantic == "completed" and route_status == "completed" and route.get("exitCode") == 0:
            successes += 1
    active = sum(end - start for start, end in intervals)
    union, peak = _interval_union(intervals)
    overlap = max(0.0, active - union)
    wall = _number((completion or {}).get("wall_seconds"))
    required_tokens: list[int] = []
    available_slots = 0
    available_inspected = 0
    calls_by_session = {session.session_id: _tool_calls(session) for session in sessions}
    for bootstrap in bootstraps:
        required = _mapping_list(bootstrap.get("requiredContext"))
        required_tokens.append(
            sum(
                (len(canonical_json(item.get("summary")).encode("utf-8")) + 3) // 4
                for item in required
                if isinstance(item.get("summary"), Mapping)
            )
        )
        available = _mapping_list(bootstrap.get("availableContext"))
        available_ids = {
            str(item["taskId"]) for item in available if isinstance(item.get("taskId"), str)
        }
        available_slots += len(available_ids)
        session_id = bootstrap.get("sessionId")
        inspected = {
            str(call["arguments"].get("task_id"))
            for call in calls_by_session.get(str(session_id), [])
            if call["name"] == "inspect_task"
            and isinstance(call.get("arguments"), Mapping)
            and call["arguments"].get("task_id") in available_ids
        }
        available_inspected += len(inspected)
    coordinator = next((session for session in sessions if session.worker_id is None), None)
    worker_sessions = [session for session in sessions if session.worker_id is not None]
    parent_usage = _usage(coordinator.entries if coordinator else ())
    worker_usage = _usage(
        entry for session in worker_sessions for entry in session.entries
    )
    worker_token_total = (worker_usage["input_tokens"] or 0) + (worker_usage["output_tokens"] or 0)
    parent_token_total = (parent_usage["input_tokens"] or 0) + (parent_usage["output_tokens"] or 0)
    denominator = len(routes)
    return {
        "worker_count": denominator,
        "worker_route_status_counts": dict(sorted(route_counts.items())),
        "worker_startup_overhead_seconds": startup,
        "median_worker_startup_overhead_seconds": _median(startup),
        "worker_active_seconds": active,
        "worker_active_union_seconds": union,
        "worker_overlap_seconds": overlap,
        "worker_overlap_ratio": overlap / active if active else None,
        "worker_peak_concurrency": peak,
        "worker_wall_utilization": union / wall if wall and wall > 0 else None,
        "worker_success_count": successes,
        "worker_semantic_failure_count": semantic_failures,
        "worker_derived_failure_count": derived_failures,
        "worker_cancellation_count": cancellations,
        "worker_failure_rate": (
            (semantic_failures + derived_failures) / denominator if denominator else None
        ),
        "worker_cancellation_rate": cancellations / denominator if denominator else None,
        "required_context_estimated_tokens": required_tokens,
        "total_required_context_estimated_tokens": sum(required_tokens),
        "available_context_reference_count": available_slots,
        "available_context_inspected_count": available_inspected,
        "available_context_inspection_rate": (
            available_inspected / available_slots if available_slots else None
        ),
        "parent_usage": parent_usage,
        "worker_usage": worker_usage,
        "worker_token_fraction": (
            worker_token_total / (worker_token_total + parent_token_total)
            if worker_token_total + parent_token_total
            else None
        ),
        "worker_cost_fraction": _ratio(
            worker_usage["cost_usd"],
            (worker_usage["cost_usd"] or 0) + (parent_usage["cost_usd"] or 0),
        ),
    }


def _tool_calls(session: _Session) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, entry in enumerate(session.entries):
        message = entry.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("type", "")).replace("_", "").lower()
            if kind not in {"toolcall", "tooluse"} or not isinstance(item.get("name"), str):
                continue
            arguments = item.get("arguments", item.get("input", {}))
            calls.append(
                {
                    "session_id": session.session_id,
                    "entry_index": index,
                    "entry_id": entry.get("id"),
                    "at": _timestamp(entry),
                    "id": str(item.get("id", item.get("toolCallId", ""))),
                    "name": item["name"],
                    "arguments": arguments if isinstance(arguments, Mapping) else {},
                }
            )
    return calls


def _join_waits(sessions: Iterable[_Session]) -> list[float]:
    waits: list[float] = []
    for session in sessions:
        calls = {call["id"]: call for call in _tool_calls(session) if call["name"] == "join_tasks"}
        for entry in session.entries:
            message = entry.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "toolResult":
                continue
            call = calls.get(str(message.get("toolCallId", "")))
            start = call.get("at") if call else None
            end = _timestamp(entry)
            if start is not None and end is not None:
                waits.append(max(0.0, end - start))
    return waits


def _usage(entries: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    totals: dict[str, float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0,
    }
    seen = Counter()
    aliases = {
        "input_tokens": ("input", "inputTokens", "input_tokens"),
        "output_tokens": ("output", "outputTokens", "output_tokens"),
        "cache_read_tokens": ("cacheRead", "cache_read", "cacheReadTokens"),
        "cache_write_tokens": ("cacheWrite", "cache_write", "cacheWriteTokens"),
    }
    for entry in entries:
        for value in _walk(entry):
            usage = value.get("usage")
            if not isinstance(usage, Mapping):
                continue
            for output, keys in aliases.items():
                number = _usage_value(usage, keys)
                if number is not None:
                    totals[output] += number
                    seen[output] += 1
            cost = usage.get("cost")
            if isinstance(cost, Mapping):
                cost = cost.get("total")
            number = _number(cost)
            if number is not None:
                totals["cost_usd"] += number
                seen["cost_usd"] += 1
    return {key: value if seen[key] else None for key, value in totals.items()}


def _usage_value(usage: Mapping[str, Any], keys: tuple[str, ...]) -> float | int | None:
    for source in (usage, usage.get("tokens")):
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = _number(source.get(key))
            if value is not None:
                return value
    return None


def _configured_features(root: Path) -> Mapping[str, Any] | None:
    value = _load_json(root / "run-spec.json")
    if not isinstance(value, Mapping):
        return None
    try:
        flags = value["variant"]["axes"]["configuration"]["extension_flags"]
    except (KeyError, TypeError):
        return None
    if not isinstance(flags, Mapping):
        return None
    names = {
        "tasks": "task-framework-tasks",
        "summaries": "task-framework-summaries",
        "compaction": "task-framework-compaction",
        "agents": "task-framework-agents",
    }
    return {key: _boolean(flags.get(flag)) for key, flag in names.items()}


def _output(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("output")
    return value if isinstance(value, Mapping) else {}


def _interval_union(intervals: list[tuple[float, float]]) -> tuple[float, int]:
    if not intervals:
        return 0.0, 0
    changes: Counter[float] = Counter()
    for start, end in intervals:
        changes[start] += 1
        changes[end] -= 1
    active = peak = 0
    previous = min(changes)
    union = 0.0
    for at in sorted(changes):
        if active > 0:
            union += at - previous
        active += changes[at]
        peak = max(peak, active)
        previous = at
    return union, peak


def _session_id(entries: Iterable[Mapping[str, Any]], fallback: str) -> str:
    for entry in entries:
        if entry.get("type") == "session" and isinstance(entry.get("id"), str):
            return str(entry["id"])
    return fallback


def _timestamp(entry: Mapping[str, Any]) -> float | None:
    value = entry.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _distribution(values: list[int]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items(), key=lambda item: int(item[0])))


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_jsonl(path: Path) -> tuple[list[Mapping[str, Any]], int]:
    entries: list[Mapping[str, Any]] = []
    errors = 0
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return entries, 1
    for raw in lines:
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            errors += 1
            continue
        if isinstance(value, Mapping):
            entries.append(value)
        else:
            errors += 1
    return entries, errors


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
