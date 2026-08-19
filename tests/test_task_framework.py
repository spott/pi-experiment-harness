from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_experiments.capture import TaskFrameworkCaptureError, capture_task_framework_runs
from pi_experiments.drivers.base import DriverError
from pi_experiments.drivers.config import process_arguments
from pi_experiments.matrix import expand_runs
from pi_experiments.metrics import extract_metrics
from pi_experiments.scenario import load_experiment

from .test_execution import make_specs


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def _custom(custom_type: str, data: dict, *, entry_id: str) -> dict:
    return {
        "type": "custom",
        "customType": custom_type,
        "data": {"schemaVersion": 1, **data},
        "id": entry_id,
        "timestamp": "2026-01-01T00:00:00Z",
    }


def _task_event(event: dict, *, entry_id: str) -> dict:
    return _custom("pi-task-framework/task-event", {"event": event}, entry_id=entry_id)


def _assistant_call(call_id: str, name: str, arguments: dict, timestamp: str) -> dict:
    return {
        "type": "message",
        "id": f"entry-{call_id}",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}
            ],
            "usage": {
                "input": 100,
                "output": 20,
                "cacheRead": 5,
                "cacheWrite": 1,
                "cost": {"total": 0.1},
            },
        },
    }


def test_extension_flags_are_explicit_and_cannot_override_harness_flags(tmp_path: Path) -> None:
    experiment_path, _ = make_specs(
        tmp_path,
        driver="json",
        steps="  - prompt: {text: extension-flags}\n",
    )
    experiment_path.write_text(
        experiment_path.read_text(encoding="utf-8").replace(
            "      extensions: []\n",
            "      extensions: []\n"
            "      extension_flags:\n"
            "        task-framework-tasks: true\n"
            "        task-framework-max-task-depth: 5\n",
        ),
        encoding="utf-8",
    )
    run = expand_runs(load_experiment(experiment_path), invocation_id="flags")[0]
    workspace = type(
        "WorkspaceStub",
        (),
        {"sessions": tmp_path / "sessions", "pi_state": tmp_path / "pi-state"},
    )()
    workspace.sessions.mkdir()
    workspace.pi_state.mkdir()

    arguments = process_arguments(run, workspace, mode="json")

    assert arguments[arguments.index("--task-framework-tasks") + 1] == "true"
    assert arguments[arguments.index("--task-framework-max-task-depth") + 1] == "5"

    run.variant.configuration["extension_flags"]["session-dir"] = "escape"
    with pytest.raises(DriverError, match="unsafe or harness-owned"):
        process_arguments(run, workspace, mode="json")


def test_capture_snapshots_private_worker_artifacts_and_rejects_symlinks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("pi_experiments.capture.task_framework.tempfile.gettempdir", lambda: str(tmp_path))
    run_id = "11111111-1111-4111-8111-111111111111"
    run = tmp_path / "pi-task-framework-runs" / run_id
    (run / "workers").mkdir(parents=True)
    (run / "sessions").mkdir()
    (run / "run.json").write_text(
        json.dumps({"schemaVersion": 1, "runId": run_id, "createdAt": 1}) + "\n"
    )
    (run / "workers" / "worker.json").write_text('{"status":"completed"}\n')
    (run / "sessions" / "worker.jsonl").write_text('{"type":"session","id":"worker"}\n')
    session = tmp_path / "session.jsonl"
    _write_jsonl(
        session,
        [
            {"type": "session", "version": 3, "id": "coordinator"},
            _custom(
                "pi-task-framework/run-context",
                {"runId": run_id, "directory": str(run)},
                entry_id="run-context",
            ),
        ],
    )

    captured = capture_task_framework_runs(session)

    assert captured[f"task-framework-runs/{run_id}/run.json"].startswith(b'{"schemaVersion"')
    assert captured[f"task-framework-runs/{run_id}/sessions/worker.jsonl"].endswith(b"\n")

    (run / "workers" / "link.json").symlink_to(run / "workers" / "worker.json")
    with pytest.raises(TaskFrameworkCaptureError, match="symlink"):
        capture_task_framework_runs(session)


def test_v2_metrics_cover_tasks_projection_workers_and_context_routing(tmp_path: Path) -> None:
    parent_id = "parent-task"
    worker_task_id = "worker-task"
    worker_id = "22222222-2222-4222-8222-222222222222"
    failed_task_id = "never-started-task"
    failed_worker_id = "44444444-4444-4444-8444-444444444444"
    run_id = "33333333-3333-4333-8333-333333333333"
    summary = {
        "objective": "inspect",
        "outcome": "done",
        "attempted": [],
        "learnings": [],
        "decisions": [],
        "files_read": [],
        "files_modified": [],
        "verification": [],
        "open_threads": [],
    }
    coordinator = [
        {"type": "session", "version": 3, "id": "coordinator"},
        _task_event(
            {
                "type": "task_created",
                "at": 1000,
                "taskId": parent_id,
                "task": "parent",
                "parentTaskId": None,
                "localDepth": 1,
                "execution": {"kind": "local", "processId": "main", "sessionId": "coordinator"},
            },
            entry_id="parent-created",
        ),
        _task_event(
            {
                "type": "output_preserved",
                "at": 1500,
                "taskId": parent_id,
                "output": {"id": "output", "taskId": parent_id, "pin": True},
            },
            entry_id="output",
        ),
        _task_event(
            {
                "type": "user_response_protected",
                "at": 1600,
                "taskId": parent_id,
                "interaction": {"id": "interaction"},
            },
            entry_id="interaction",
        ),
        _assistant_call("inspect-parent", "inspect_task", {"task_id": parent_id}, "2026-01-01T00:00:01Z"),
        _assistant_call("read-output", "read_preserved_output", {"output_id": "output"}, "2026-01-01T00:00:02Z"),
        _assistant_call("spawn", "spawn_task", {"task": "worker"}, "2026-01-01T00:00:03Z"),
        _assistant_call("join", "join_tasks", {"task_ids": [worker_task_id]}, "2026-01-01T00:00:04Z"),
        {
            "type": "message",
            "id": "join-result",
            "timestamp": "2026-01-01T00:00:06Z",
            "message": {"role": "toolResult", "toolCallId": "join", "content": "joined"},
        },
        _custom(
            "pi-task-framework/evaluation",
            {
                "kind": "context_projection",
                "at": 4500,
                "sessionId": "coordinator",
                "projectedTaskIds": [worker_task_id],
                "projectionRejections": [{"taskId": "other", "reasons": ["ambiguous"]}],
                "metrics": {
                    "inputMessageCount": 20,
                    "outputMessageCount": 8,
                    "inputEstimatedTokens": 1000,
                    "outputEstimatedTokens": 300,
                    "inputSerializedBytes": 4000,
                    "outputSerializedBytes": 1200,
                    "projectedRawMessageCount": 12,
                    "pinnedClosureEntryCount": 3,
                    "pinnedClosureEstimatedTokens": 40,
                    "protectedInteractionCount": 1,
                    "replayedMessageCount": 1,
                    "maxReplayCascadeDepth": 2,
                },
            },
            entry_id="projection",
        ),
        _custom(
            "pi-task-framework/evaluation",
            {
                "kind": "global_compaction",
                "at": 4600,
                "sessionId": "coordinator",
                "reason": "manual",
                "willRetry": False,
                "cancelled": True,
                "requestedFirstKeptEntryId": "x",
                "projectedTaskIds": [],
                "projectionRejections": [],
                "tokensBefore": 1000,
            },
            entry_id="compaction",
        ),
        _task_event(
            {
                "type": "task_completed",
                "at": 5000,
                "taskId": parent_id,
                "summary": summary,
            },
            entry_id="parent-completed",
        ),
    ]
    _write_jsonl(tmp_path / "session.jsonl", coordinator)
    _write_jsonl(tmp_path / "events.jsonl", [])
    (tmp_path / "run-spec.json").write_text(
        json.dumps(
            {
                "variant": {
                    "axes": {
                        "configuration": {
                            "extension_flags": {
                                "task-framework-tasks": True,
                                "task-framework-summaries": True,
                                "task-framework-compaction": True,
                                "task-framework-agents": True,
                            }
                        }
                    }
                }
            }
        )
    )
    run = tmp_path / "task-framework-runs" / run_id
    worker_session = [
        {"type": "session", "version": 3, "id": "worker-session"},
        _task_event(
            {
                "type": "task_created",
                "at": 2000,
                "taskId": worker_task_id,
                "task": "worker",
                "parentTaskId": parent_id,
                "localDepth": 0,
                "execution": {
                    "kind": "worker",
                    "workerId": worker_id,
                    "processId": f"worker:{worker_id}",
                    "sessionId": "worker-session",
                    "agentDepth": 1,
                },
            },
            entry_id="worker-created",
        ),
        _task_event(
            {"type": "task_started", "at": 2500, "taskId": worker_task_id},
            entry_id="worker-started",
        ),
        _assistant_call("inspect-available", "inspect_task", {"task_id": parent_id}, "2026-01-01T00:00:03Z"),
        _task_event(
            {
                "type": "task_completed",
                "at": 4000,
                "taskId": worker_task_id,
                "summary": summary,
            },
            entry_id="worker-completed",
        ),
    ]
    _write_jsonl(run / "sessions" / f"{worker_id}.jsonl", worker_session)
    _write_jsonl(
        run / "sessions" / f"{failed_worker_id}.jsonl",
        [
            {"type": "session", "version": 3, "id": "failed-worker-session"},
            _task_event(
                {
                    "type": "task_created",
                    "at": 3000,
                    "taskId": failed_task_id,
                    "task": "never started",
                    "parentTaskId": parent_id,
                    "localDepth": 0,
                    "execution": {
                        "kind": "worker",
                        "workerId": failed_worker_id,
                        "processId": f"worker:{failed_worker_id}",
                        "sessionId": "failed-worker-session",
                        "agentDepth": 1,
                    },
                },
                entry_id="failed-worker-created",
            ),
        ],
    )
    (run / "workers").mkdir()
    (run / "workers" / f"{worker_id}.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workerId": worker_id,
                "taskId": worker_task_id,
                "runId": run_id,
                "sessionId": "worker-session",
                "sessionFile": "/captured/worker.jsonl",
                "spawningSessionId": "coordinator",
                "parentTaskId": parent_id,
                "status": "completed",
                "exitCode": 0,
                "startedAt": 2500,
                "exitedAt": 4500,
            }
        )
    )
    (run / "workers" / f"{failed_worker_id}.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workerId": failed_worker_id,
                "taskId": failed_task_id,
                "runId": run_id,
                "sessionId": "failed-worker-session",
                "sessionFile": "/captured/failed-worker.jsonl",
                "spawningSessionId": "coordinator",
                "parentTaskId": parent_id,
                "status": "failed",
                "exitCode": 1,
                "exitedAt": 3200,
            }
        )
    )
    (run / "bootstraps").mkdir()
    (run / "bootstraps" / f"{worker_id}.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workerId": worker_id,
                "taskId": worker_task_id,
                "sessionId": "worker-session",
                "requiredContext": [{"taskId": "prior", "summary": summary}],
                "availableContext": [{"taskId": parent_id}],
            }
        )
    )

    metrics = extract_metrics(tmp_path, {"status": "completed", "wall_seconds": 10})
    task = metrics["namespaces"]["task_framework"]

    assert metrics["extractor_errors"] == []
    assert task["configured_features"] == {
        "tasks": True,
        "summaries": True,
        "compaction": True,
        "agents": True,
    }
    assert task["task_count"] == 3
    assert task["semantic_depth_distribution"] == {"1": 1, "2": 2}
    assert task["local_depth_distribution"] == {"0": 2, "1": 1}
    assert task["agent_depth_distribution"] == {"0": 1, "1": 2}
    assert task["pinned_output_count"] == 1
    assert task["protected_interaction_count"] == 1
    assert task["latest_context_compression_ratio"] == 0.3
    assert task["max_pin_retained_closure_estimated_tokens"] == 40
    assert task["replayed_message_occurrences"] == 1
    assert task["max_replay_cascade_depth"] == 2
    assert task["projection_rejection_reasons"] == {"ambiguous": 1}
    assert task["global_compaction_cancelled_count"] == 1
    assert task["join_wait_seconds"] == [2.0]
    assert task["worker_startup_overhead_seconds"] == [0.5]
    assert task["worker_active_seconds"] == 2.0
    assert task["worker_success_count"] == 1
    assert task["worker_derived_failure_count"] == 1
    assert task["worker_failure_rate"] == 0.5
    assert task["available_context_inspection_rate"] == 1.0
    assert task["total_required_context_estimated_tokens"] > 0
    assert task["parent_usage"]["input_tokens"] == 400
    assert task["worker_usage"]["input_tokens"] == 100
