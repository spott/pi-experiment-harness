from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pi_experiments.cli import collect_garbage, compare_experiment, run_experiment, show_run
from pi_experiments.metrics import extract_metrics

from .test_execution import make_specs


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def test_generic_and_task_compaction_extractors(tmp_path: Path) -> None:
    (tmp_path / "turns").mkdir()
    (tmp_path / "turns" / "0001.json").write_text(
        json.dumps(
            {
                "turn_index": 1,
                "scenario_step": 0,
                "elapsed_seconds": 1.5,
                "usage": {
                    "tokens": {"input": 120, "output": 10},
                    "cost": 0.03,
                    "contextUsage": {"tokens": 120, "contextWindow": 1000, "percent": 12},
                },
            }
        )
    )
    session = [
        {"type": "session", "version": 3},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "begin-call",
                        "name": "begin_task",
                        "arguments": {"objective": "inspect"},
                    }
                ],
                "usage": {"input": 10, "output": 2, "cacheRead": 3, "cost": {"total": 0.01}},
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "begin-call",
                "details": {
                    "schemaVersion": 1,
                    "event": "begin",
                    "taskId": "task-1",
                    "objective": "inspect",
                    "toolCallId": "begin-call",
                },
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "read-call", "name": "read", "arguments": {}}],
                "usage": {"input": 20, "output": 4, "cacheWrite": 5, "cost": {"total": 0.02}},
            },
        },
        {
            "type": "message",
            "message": {"role": "toolResult", "toolCallId": "read-call", "content": "x" * 500},
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "end-call",
                        "name": "end_task",
                        "arguments": {"task_id": "task-1"},
                    }
                ],
                "usage": {"input": 30, "output": 6, "cost": {"total": 0.03}},
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "end-call",
                "details": {
                    "schemaVersion": 1,
                    "event": "end",
                    "taskId": "task-1",
                    "accepted": True,
                    "summary": {"objective": "inspect", "outcome": "found it"},
                },
            },
        },
        {"type": "compaction", "reason": "manual", "usage": {"input": 7, "output": 1, "cost": 0.01}},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "expand-call",
                        "name": "expand_task",
                        "arguments": {"task_id": "task-1"},
                    }
                ],
            },
        },
    ]
    _write_jsonl(tmp_path / "session.jsonl", session)
    _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {"type": "compaction_start", "reason": "manual"},
            {"type": "compaction_end", "reason": "manual"},
        ],
    )
    (tmp_path / "changed-files.json").write_text('[{"status":"M","path":"a.py"}]')
    (tmp_path / "patch.diff").write_text("--- a/a.py\n+++ b/a.py\n-old\n+new\n+more\n")
    (tmp_path / "final-tree.txt").write_text("abc123\n")
    (tmp_path / "evaluator.json").write_text('[{"passed":true}]')

    metrics = extract_metrics(tmp_path, {"status": "completed", "wall_seconds": 2.0})
    generic = metrics["namespaces"]["generic"]
    task = metrics["namespaces"]["task_compaction"]

    assert metrics["extractor_errors"] == []
    assert generic["input_tokens"] == 67
    assert generic["output_tokens"] == 13
    assert generic["cache_read_tokens"] == 3
    assert generic["cache_write_tokens"] == 5
    assert generic["cost_usd"] == 0.07
    assert generic["tool_call_count"] == 4
    assert generic["global_compaction_count"] == 1
    assert generic["lines_added"] == 2 and generic["lines_deleted"] == 1
    assert task["begin_task_count"] == 1
    assert task["closed_task_count"] == 1
    assert task["accepted_region_count"] == 1
    assert task["expand_task_count"] == 1
    assert task["expanded_task_ids"] == ["task-1"]
    assert task["regions"][0]["compression_ratio"] > 1
    assert task["regions"][0]["global_compactions_after_region"] == 1
    assert task["raw_task_bodies_reintroduced_after_global_compaction"] is None


def test_cli_run_show_compare_workflow(tmp_path: Path) -> None:
    experiment_path, _ = make_specs(
        tmp_path,
        driver="json",
        steps="""  - prompt: {text: cli-smoke}
  - evaluate:
      name: file-check
      commands: grep -q cli-smoke agent.txt
  - checkpoint: final
""",
    )
    results_dir = tmp_path / "results-override"

    summary = run_experiment(
        experiment_path,
        invocation_id="cli-smoke",
        results_dir=results_dir,
    )
    run_id = summary["runs"][0]["run_id"]
    shown = show_run(results_dir, run_id[:16])
    compared = compare_experiment(results_dir, summary["experiment_id"][:12])

    assert summary["statuses"] == {"completed": 1}
    assert shown["run"]["id"] == run_id
    assert shown["metrics"]["generic"]["evaluation_count"] == 1
    assert shown["metrics"]["generic"]["status"] == "completed"
    assert any(item["path"] == "metrics.json" for item in shown["artifacts"])
    assert compared["run_count"] == 1
    assert compared["variants"][0]["statuses"] == {"completed": 1}
    assert compared["variants"][0]["metrics"]["generic.evaluations_passed"]["median"] == 1


def test_gc_is_dry_run_by_default(tmp_path: Path) -> None:
    stale = tmp_path / "piexp-stale"
    stale.mkdir()
    old = time.time() - 7200
    os.utime(stale, (old, old))

    preview = collect_garbage(tmp_path, older_than_hours=1)

    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 1
    assert stale.is_dir()
