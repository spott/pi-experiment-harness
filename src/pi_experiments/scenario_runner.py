"""Driver-independent execution of resolved scenario steps."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .capture.git import checkpoint_git
from .drivers.base import PiDriver, UnsupportedDriverOperation
from .drivers.config import process_environment
from .evaluators.command import CommandEvaluation, evaluate_commands
from .paths import slugify
from .spec import RunSpec
from .storage import ResultIndex, ResultStore
from .workspace.base import Workspace


class BudgetExceeded(RuntimeError):
    def __init__(self, limits: Mapping[str, float], observed: Mapping[str, float]):
        super().__init__("scenario budget exceeded")
        self.limits = dict(limits)
        self.observed = dict(observed)


@dataclass
class ScenarioOutcome:
    steps_completed: int = 0
    turns: list[Mapping[str, Any]] = field(default_factory=list)
    checkpoints: list[Mapping[str, Any]] = field(default_factory=list)
    evaluations: list[CommandEvaluation] = field(default_factory=list)
    final_response: str = ""
    last_stats: Mapping[str, Any] = field(default_factory=dict)


class ScenarioRunner:
    def __init__(
        self,
        run: RunSpec,
        workspace: Workspace,
        driver: PiDriver,
        store: ResultStore,
        index: ResultIndex | None = None,
    ):
        self.run = run
        self.workspace = workspace
        self.driver = driver
        self.store = store
        self.index = index
        self.started = time.monotonic()
        self.deadline = self.started + run.options.timeout_seconds
        self.limits: dict[str, float] = {}
        self.outcome = ScenarioOutcome()

    def execute(self) -> ScenarioOutcome:
        self._validate_capabilities()
        self.driver.start()
        for step_index, step in enumerate(self.run.scenario.steps):
            timeout = self._remaining()
            operation = step.operation
            arguments = step.arguments
            if operation == "prompt":
                self._prompt(str(arguments["text"]), step_index, timeout, source="scenario")
            elif operation == "checkpoint":
                self._checkpoint(str(arguments["name"]), step_index, timeout)
            elif operation == "evaluate":
                self._evaluate(arguments, step_index, timeout)
            elif operation == "compact":
                result = self.driver.compact(arguments, timeout=timeout)
                self.store.write_json(
                    self.run, f"turns/step-{step_index + 1:04d}-compact.json", result
                )
            elif operation == "restart_pi":
                self.driver.restart(timeout=timeout)
            elif operation == "fork":
                entry_id = arguments.get("entry_id", arguments.get("entryId"))
                if not isinstance(entry_id, str) or not entry_id:
                    raise ValueError("fork requires entry_id")
                result = self.driver.fork(entry_id, timeout=timeout)
                self.store.write_json(
                    self.run, f"turns/step-{step_index + 1:04d}-fork.json", result
                )
            elif operation == "switch_branch":
                raise UnsupportedDriverOperation(
                    "Pi 0.84 RPC cannot switch the active leaf to an arbitrary tree entry"
                )
            elif operation == "budget":
                self.limits.update({key: float(value) for key, value in arguments.items()})
            else:
                raise ValueError(f"unknown scenario operation: {operation}")
            self.outcome.steps_completed = step_index + 1
            self._check_budget()
        return self.outcome

    def _validate_capabilities(self) -> None:
        if self.run.options.isolation == "strict":
            return
        if any(
            step.operation == "evaluate" and step.arguments.get("hidden")
            for step in self.run.scenario.steps
        ):
            raise UnsupportedDriverOperation(
                "hidden evaluators require strict isolation, which is not implemented"
            )

    def _prompt(
        self, text: str, step_index: int, timeout: float, *, source: str
    ) -> None:
        turn = self.driver.prompt(text, timeout=timeout)
        turn_index = len(self.outcome.turns) + 1
        record = {
            "turn_index": turn_index,
            "scenario_step": step_index,
            "source": source,
            "elapsed_seconds": turn.elapsed_seconds,
            "response_text": turn.response_text,
            "usage": turn.usage,
        }
        self.outcome.turns.append(record)
        self.outcome.final_response = turn.response_text
        self.outcome.last_stats = turn.usage
        self.store.write_json(self.run, f"turns/{turn_index:04d}.json", record)
        if self.index is not None:
            self.index.add_turn(self.run.run_id, turn_index, record)

    def _checkpoint(self, name: str, step_index: int, timeout: float) -> None:
        try:
            driver_checkpoint = self.driver.checkpoint(timeout=timeout)
            pi: Mapping[str, Any] = {
                "state": driver_checkpoint.state,
                "stats": driver_checkpoint.stats,
                "entries": driver_checkpoint.entries,
                "tree": driver_checkpoint.tree,
            }
            self.outcome.last_stats = driver_checkpoint.stats
        except UnsupportedDriverOperation as exc:
            pi = {"available": False, "reason": str(exc)}
        record = {
            "name": name,
            "scenario_step": step_index,
            "wall_seconds": time.monotonic() - self.started,
            "git": checkpoint_git(self.workspace.repository, self.workspace.baseline_sha),
            "pi": pi,
        }
        self.outcome.checkpoints.append(record)
        filename = f"{step_index + 1:04d}-{slugify(name)}.json"
        self.store.write_json(self.run, f"checkpoints/{filename}", record)
        if self.index is not None:
            self.index.add_checkpoint(self.run.run_id, name, step_index, record)

    def _evaluate(self, arguments: Mapping[str, Any], step_index: int, timeout: float) -> None:
        evaluation_timeout = min(float(arguments.get("timeout_seconds", timeout)), timeout)
        evaluation = evaluate_commands(
            str(arguments["name"]),
            tuple(str(command) for command in arguments["commands"]),
            repository=self.workspace.repository,
            environment=process_environment(self.run, self.workspace),
            timeout_seconds=evaluation_timeout,
        )
        self.outcome.evaluations.append(evaluation)
        if self.index is not None:
            exit_statuses = [item.get("exit_status") for item in evaluation.commands]
            combined_status = 0 if evaluation.passed else next(
                (status for status in exit_statuses if status not in {0, None}), None
            )
            self.index.add_evaluation(
                self.run.run_id,
                evaluation.name,
                step_index,
                combined_status,
                evaluation,
            )
        if arguments.get("inject_result"):
            rendered = _render_evaluation(evaluation)
            self._prompt(rendered, step_index, self._remaining(), source="evaluator_feedback")

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded(
                {"max_wall_seconds": self.run.options.timeout_seconds},
                {"wall_seconds": time.monotonic() - self.started},
            )
        if "max_wall_seconds" in self.limits:
            remaining = min(
                remaining,
                self.limits["max_wall_seconds"] - (time.monotonic() - self.started),
            )
            if remaining <= 0:
                self._check_budget()
        return remaining

    def _check_budget(self) -> None:
        observed = {
            "wall_seconds": time.monotonic() - self.started,
            "model_turns": float(len(self.outcome.turns)),
            "cost_usd": _cost(self.outcome.last_stats),
        }
        exceeded = (
            ("max_wall_seconds" in self.limits and observed["wall_seconds"] > self.limits["max_wall_seconds"])
            or ("max_model_turns" in self.limits and observed["model_turns"] > self.limits["max_model_turns"])
            or ("max_cost_usd" in self.limits and observed["cost_usd"] > self.limits["max_cost_usd"])
        )
        if exceeded:
            raise BudgetExceeded(self.limits, observed)


def _cost(stats: Mapping[str, Any]) -> float:
    value = stats.get("cost", 0)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        total = value.get("total", 0)
        return float(total) if isinstance(total, (int, float)) else 0.0
    return 0.0


def _render_evaluation(evaluation: CommandEvaluation) -> str:
    lines = [
        f"External evaluator {evaluation.name!r} completed.",
        f"Passed: {evaluation.passed}",
        "Treat this as explicit user-provided test feedback and continue the task.",
    ]
    for command in evaluation.commands:
        lines.extend(
            [
                f"\n$ {command['command']}",
                f"exit_status: {command['exit_status']}",
                "stdout:",
                str(command["stdout"]),
                "stderr:",
                str(command["stderr"]),
            ]
        )
    return "\n".join(lines)
