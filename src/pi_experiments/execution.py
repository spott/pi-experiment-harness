"""End-to-end execution of one resolved run."""

from __future__ import annotations

import dataclasses
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Mapping

from .capture.git import capture_git
from .drivers.base import PiDriver, make_driver
from .drivers.config import pi_provenance
from .metrics import extract_metrics
from .scenario_runner import BudgetExceeded, ScenarioOutcome, ScenarioRunner
from .serde import content_hash, to_primitive
from .spec import RunSpec
from .storage import ResultIndex, ResultStore
from .workspace.base import Workspace, WorkspaceBackend
from .workspace.worktree import FastWorktreeBackend


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RunResult:
    run: RunSpec
    status: str
    exit_reason: str
    process_exit_status: int | None
    steps_completed: int
    wall_seconds: float
    result_path: Path
    error: str | None = None


class RunExecutor:
    """Owns workspace lifecycle, raw capture, and result finalization for one run."""

    def __init__(
        self,
        store: ResultStore,
        index: ResultIndex | None = None,
        backend: WorkspaceBackend | None = None,
    ):
        self.store = store
        self.index = index
        self.backend = backend or FastWorktreeBackend()

    def execute(self, requested_run: RunSpec) -> RunResult:
        started_at = _now()
        clock_started = monotonic()
        workspace: Workspace | None = None
        driver: PiDriver | None = None
        run = requested_run
        outcome = ScenarioOutcome()
        status = "failed"
        exit_reason = "setup_failed"
        error: str | None = None
        bundle_created = False
        locks = {"events": Lock(), "stderr": Lock()}
        try:
            workspace = self.backend.prepare(requested_run)
            provenance = {
                **dict(requested_run.provenance),
                **dict(workspace.provenance),
                **pi_provenance(requested_run, workspace),
                "started_at": started_at,
            }
            run = dataclasses.replace(
                requested_run,
                resolved_revision=workspace.baseline_sha,
                provenance=provenance,
            )
            paths = self.store.create_run(run)
            bundle_created = True
            if self.index is not None:
                self.index.register_run(run, paths.root, status="running")
                self.index.update_run(run.run_id, status="running", started_at=started_at)

            def event_sink(event: Mapping[str, Any]) -> None:
                with locks["events"]:
                    self.store.append_jsonl(run, "events.jsonl", [event])

            def stderr_sink(data: bytes) -> None:
                with locks["stderr"]:
                    self.store.append_bytes(run, "stderr.log", data)

            driver = make_driver(run, workspace, event_sink, stderr_sink)
            scenario_runner = ScenarioRunner(
                run, workspace, driver, self.store, self.index
            )
            try:
                scenario_runner.execute()
            finally:
                outcome = scenario_runner.outcome
            status = "completed"
            exit_reason = "scenario_completed"
        except BudgetExceeded as exc:
            status = "budget_exceeded"
            exit_reason = "budget_exceeded"
            error = str(exc)
            if bundle_created:
                self.store.write_json(
                    run,
                    "budget.json",
                    {"limits": exc.limits, "observed": exc.observed},
                )
        except Exception as exc:  # A failed run is still a durable experimental outcome.
            status = "failed"
            exit_reason = type(exc).__name__
            error = str(exc)
            failure = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            if not bundle_created:
                try:
                    self.store.create_run(run)
                    bundle_created = True
                    if self.index is not None:
                        self.index.register_run(
                            run,
                            self.store.run_dir(run.experiment_id, run.run_id),
                            status="running",
                        )
                        self.index.update_run(
                            run.run_id, status="running", started_at=started_at
                        )
                except Exception as persistence_exc:
                    error = f"{error}; failed to persist setup failure: {persistence_exc}"
            if bundle_created:
                self.store.write_json(run, "error.json", failure)
        finally:
            if driver is not None:
                try:
                    driver.stop()
                except Exception as exc:
                    if status == "completed":
                        status = "failed"
                        exit_reason = "driver_stop_failed"
                        error = str(exc)
            if bundle_created:
                try:
                    self._capture_final(run, workspace, driver, outcome)
                except Exception as exc:
                    status = "failed"
                    exit_reason = "artifact_capture_failed"
                    error = str(exc)
                    self._write_error_fallback(run, exc)
            if workspace is not None:
                try:
                    self.backend.cleanup(workspace)
                except Exception as exc:
                    if status == "completed":
                        status = "failed"
                        exit_reason = "workspace_cleanup_failed"
                        error = str(exc)
            ended_at = _now()
            wall_seconds = monotonic() - clock_started
            process_status = driver.returncode if driver is not None else None
            if bundle_created:
                completion = {
                    "status": status,
                    "exit_reason": exit_reason,
                    "process_exit_status": process_status,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "wall_seconds": wall_seconds,
                    "steps_completed": outcome.steps_completed,
                    "error": error,
                }
                metrics = extract_metrics(
                    self.store.run_dir(run.experiment_id, run.run_id), completion
                )
                self.store.write_json(run, "metrics.json", metrics)
                if self.index is not None:
                    for namespace, values in metrics["namespaces"].items():
                        self.index.add_metrics(run.run_id, namespace, values)
                try:
                    self.store.finalize(run, completion)
                except FileExistsError:
                    pass
                if self.index is not None:
                    self._index_artifacts(run)
                    self.index.update_run(
                        run.run_id,
                        status=status,
                        exit_reason=exit_reason,
                        process_exit_status=process_status,
                        ended_at=ended_at,
                    )
        result_path = self.store.run_dir(run.experiment_id, run.run_id)
        return RunResult(
            run=run,
            status=status,
            exit_reason=exit_reason,
            process_exit_status=driver.returncode if driver is not None else None,
            steps_completed=outcome.steps_completed,
            wall_seconds=monotonic() - clock_started,
            result_path=result_path,
            error=error,
        )

    def _capture_final(
        self,
        run: RunSpec,
        workspace: Workspace | None,
        driver: PiDriver | None,
        outcome: ScenarioOutcome,
    ) -> None:
        paths = self.store.paths(run.experiment_id, run.run_id)
        if not paths.events.exists():
            self.store.write_bytes(run, "events.jsonl", b"")
        if not paths.stderr.exists():
            self.store.write_bytes(run, "stderr.log", b"")
        if driver is not None and driver.session_file is not None and driver.session_file.is_file():
            self.store.write_bytes(run, "session.jsonl", driver.session_file.read_bytes())
        elif not paths.session.exists():
            self.store.write_bytes(run, "session.jsonl", b"")
        self.store.write_bytes(
            run, "final-response.md", outcome.final_response.encode("utf-8")
        )
        self.store.write_json(run, "evaluator.json", outcome.evaluations)
        stdout = []
        stderr = []
        for evaluation in outcome.evaluations:
            for command in evaluation.commands:
                stdout.append(f"## {evaluation.name}: {command['command']}\n{command['stdout']}")
                stderr.append(f"## {evaluation.name}: {command['command']}\n{command['stderr']}")
        self.store.write_bytes(run, "evaluator-stdout.log", "\n".join(stdout).encode())
        self.store.write_bytes(run, "evaluator-stderr.log", "\n".join(stderr).encode())
        if workspace is not None:
            for name, data in capture_git(workspace.repository, workspace.baseline_sha).items():
                self.store.write_bytes(run, name, data)

    def _index_artifacts(self, run: RunSpec) -> None:
        if self.index is None:
            return
        root = self.store.run_dir(run.experiment_id, run.run_id)
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            self.index.add_artifact(
                run.run_id,
                relative,
                _artifact_kind(relative),
                content_hash(data),
                len(data),
            )

    def _write_error_fallback(self, run: RunSpec, exc: Exception) -> None:
        root = self.store.run_dir(run.experiment_id, run.run_id)
        if not (root / "capture-error.json").exists():
            try:
                self.store.write_json(
                    run,
                    "capture-error.json",
                    {"type": type(exc).__name__, "message": str(exc)},
                )
            except Exception:
                pass


def _artifact_kind(relative: str) -> str:
    if relative == "events.jsonl":
        return "events"
    if relative == "session.jsonl":
        return "session"
    if relative.startswith("turns/"):
        return "turn"
    if relative.startswith("checkpoints/"):
        return "checkpoint"
    if relative.startswith("evaluator"):
        return "evaluation"
    if relative in {"patch.diff", "git-status.txt", "changed-files.json", "final-tree.txt"}:
        return "git"
    return "artifact"
