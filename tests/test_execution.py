from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from pi_experiments.evaluators import command as evaluator_command
from pi_experiments.execution import RunExecutor
from pi_experiments.matrix import expand_runs
from pi_experiments.scenario import load_experiment
from pi_experiments.storage import ResultIndex, ResultStore
from pi_experiments.workspace import FastWorktreeBackend


FAKE_PI = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("fake-pi 1.0")
    raise SystemExit(0)

def arg(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default

mode = arg("--mode")
session_dir = Path(arg("--session-dir"))
session_dir.mkdir(parents=True, exist_ok=True)
session = Path(arg("--session", session_dir / "fake-session.jsonl"))
if not session.exists():
    session.write_text(json.dumps({"type": "session", "version": 3, "id": "fake"}) + "\n")

def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

def assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "usage": {"input": 10, "output": 2, "cacheRead": 1, "cacheWrite": 0, "cost": {"total": 0.01}}}

def do_prompt(text):
    with open("agent.txt", "a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    message = assistant("done: " + text)
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "message": message}) + "\n")
    emit({"type": "agent_start"})
    emit({"type": "turn_end", "message": message, "toolResults": []})
    emit({"type": "message_end", "message": message})
    emit({"type": "agent_end", "messages": [message], "willRetry": False})
    emit({"type": "agent_settled"})

if mode == "json":
    prompt = sys.argv[-1]
    do_prompt(prompt)
    raise SystemExit(0)

for raw in sys.stdin.buffer:
    request = json.loads(raw)
    request_id = request.get("id")
    command = request["type"]
    def response(data=None, success=True):
        value = {"id": request_id, "type": "response", "command": command, "success": success}
        if data is not None:
            value["data"] = data
        emit(value)
    if command == "prompt":
        response({})
        do_prompt(request["message"])
    elif command == "get_state":
        response({"sessionFile": str(session.resolve()), "sessionId": "fake", "isStreaming": False})
    elif command == "get_session_stats":
        response({"assistantMessages": 2, "tokens": {"input": 20, "output": 4}, "cost": 0.02, "contextUsage": {"tokens": 20, "contextWindow": 1000, "percent": 2}})
    elif command == "get_entries":
        response({"entries": [{"id": "entry-1", "type": "message"}], "leafId": "entry-1"})
    elif command == "get_tree":
        response({"tree": [], "leafId": "entry-1"})
    elif command == "compact":
        emit({"type": "compaction_start", "reason": "manual"})
        emit({"type": "compaction_end", "reason": "manual"})
        response({"summary": "compact", "tokensBefore": 20})
    elif command == "fork":
        response({"text": "forked", "cancelled": False})
    else:
        response({"message": "unsupported"}, success=False)
'''


def git(*arguments: str, cwd: Path) -> str:
    process = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, stdout=subprocess.PIPE, text=True
    )
    return process.stdout.strip()


def make_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir(parents=True)
    git("init", "-q", cwd=repository)
    git("config", "user.email", "test@example.com", cwd=repository)
    git("config", "user.name", "Test", cwd=repository)
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    git("add", ".", cwd=repository)
    git("commit", "-qm", "baseline", cwd=repository)
    return repository


def make_specs(root: Path, *, driver: str, steps: str) -> tuple[Path, Path]:
    repository = make_repository(root)
    fake_pi = root / "fake-pi"
    fake_pi.write_text(FAKE_PI, encoding="utf-8")
    fake_pi.chmod(0o755)
    scenario = root / "scenario.yaml"
    scenario.write_text(
        f"""name: execution-test
repo:
  source: {repository}
  revision: HEAD
driver: {driver}
steps:
{steps}
""",
        encoding="utf-8",
    )
    experiment = root / "experiment.yaml"
    experiment.write_text(
        f"""name: execution
scenario: scenario.yaml
repetitions: 1
matrix:
  model:
    - id: fake/model
      thinking: low
  configuration:
    - name: vanilla
      extensions: []
run:
  isolation: fast
  timeout_seconds: 30
  results_dir: results
  pi_executable: {fake_pi}
  credential_source: none
""",
        encoding="utf-8",
    )
    return experiment, repository


def execute(root: Path, experiment_path: Path):
    experiment = load_experiment(experiment_path)
    run = expand_runs(experiment, invocation_id="execution-test")[0]
    store = ResultStore(root / "results")
    index = ResultIndex(root / "results" / "index.sqlite3")
    store.initialize_experiment(run.experiment_id, experiment, run.scenario)
    index.register_experiment(run.experiment_id, experiment, run.scenario)
    index.register_variant(run.experiment_id, run.variant)
    executor = RunExecutor(
        store,
        index,
        backend=FastWorktreeBackend(temporary_root=root / "workspaces"),
    )
    (root / "workspaces").mkdir()
    return executor.execute(run), index


def test_evaluator_timeout_is_shared_across_commands(tmp_path: Path, monkeypatch) -> None:
    clock = iter((0.0, 0.1, 0.3, 0.7, 0.8, 0.9))
    observed_timeouts: list[float] = []

    def fake_run(*args, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(evaluator_command.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(evaluator_command.subprocess, "run", fake_run)

    result = evaluator_command.evaluate_commands(
        "bounded",
        ("first", "second"),
        repository=tmp_path,
        environment={},
        timeout_seconds=1.0,
    )

    assert result.passed is True
    assert [round(value, 1) for value in observed_timeouts] == [0.9, 0.3]


def test_rpc_scenario_captures_and_cleans_worktree(tmp_path: Path) -> None:
    experiment_path, repository = make_specs(
        tmp_path,
        driver="rpc",
        steps="""  - prompt: {text: first}
  - checkpoint: after-first
  - evaluate:
      name: file-check
      commands: test -f agent.txt
  - restart_pi: {}
  - prompt: {text: second}
  - compact: {}
  - checkpoint: final
""",
    )

    result, index = execute(tmp_path, experiment_path)

    assert result.status == "completed"
    assert result.steps_completed == 7
    assert result.run.resolved_revision == git("rev-parse", "HEAD", cwd=repository)
    assert not (repository / "agent.txt").exists()
    assert json.loads((result.result_path / "completed.json").read_text())["status"] == "completed"
    assert "first" in (result.result_path / "patch.diff").read_text()
    assert "second" in (result.result_path / "patch.diff").read_text()
    assert (result.result_path / "session.jsonl").stat().st_size > 0
    assert len(list((result.result_path / "turns").glob("[0-9]*.json"))) == 2
    assert len(list((result.result_path / "checkpoints").glob("*.json"))) == 2
    assert json.loads((result.result_path / "evaluator.json").read_text())[0]["passed"] is True
    assert "compaction_start" in (result.result_path / "events.jsonl").read_text()
    assert not any((tmp_path / "workspaces").iterdir())
    row = index.get_run(result.run.run_id)
    assert row is not None and row["status"] == "completed"


def test_budget_termination_is_a_finalized_outcome(tmp_path: Path) -> None:
    experiment_path, _ = make_specs(
        tmp_path,
        driver="rpc",
        steps="""  - prompt: {text: first}
  - budget: {max_model_turns: 1}
  - prompt: {text: over-budget}
""",
    )

    result, index = execute(tmp_path, experiment_path)

    assert result.status == "budget_exceeded"
    assert result.steps_completed == 3
    assert (result.result_path / "budget.json").is_file()
    assert json.loads((result.result_path / "completed.json").read_text())["status"] == "budget_exceeded"
    assert index.get_run(result.run.run_id)["status"] == "budget_exceeded"


def test_setup_failure_is_a_finalized_indexed_outcome(tmp_path: Path) -> None:
    experiment_path, repository = make_specs(
        tmp_path,
        driver="json",
        steps="  - prompt: {text: never-runs}\n",
    )
    shutil.rmtree(repository)

    result, index = execute(tmp_path, experiment_path)

    assert result.status == "failed"
    assert result.exit_reason == "WorkspaceError"
    assert (result.result_path / "error.json").is_file()
    assert (result.result_path / "metrics.json").is_file()
    assert json.loads((result.result_path / "completed.json").read_text())["status"] == "failed"
    assert index.get_run(result.run.run_id)["status"] == "failed"


def test_fast_isolation_rejects_hidden_evaluator(tmp_path: Path) -> None:
    experiment_path, _ = make_specs(
        tmp_path,
        driver="json",
        steps="""  - prompt: {text: one-shot}
  - evaluate:
      name: hidden-tests
      commands: test -f agent.txt
      hidden: true
""",
    )

    result, _ = execute(tmp_path, experiment_path)

    assert result.status == "failed"
    assert result.exit_reason == "UnsupportedDriverOperation"
    assert "strict isolation" in (result.error or "")


def test_json_scenario_runs_evaluator_and_degraded_checkpoint(tmp_path: Path) -> None:
    experiment_path, _ = make_specs(
        tmp_path,
        driver="json",
        steps="""  - prompt: {text: one-shot}
  - evaluate:
      name: file-check
      commands: grep -q one-shot agent.txt
  - checkpoint: final
""",
    )

    result, _ = execute(tmp_path, experiment_path)

    assert result.status == "completed"
    assert (result.result_path / "final-response.md").read_text() == "done: one-shot"
    checkpoint = json.loads(next((result.result_path / "checkpoints").glob("*.json")).read_text())
    assert checkpoint["pi"]["available"] is False
    assert json.loads((result.result_path / "evaluator.json").read_text())[0]["passed"] is True
