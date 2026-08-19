"""Scenario and experiment document loading."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any, Mapping

import yaml

from .paths import relative_to
from .serde import content_hash
from .spec import (
    ExperimentSpec,
    RepositorySpec,
    RunOptions,
    ScenarioSpec,
    ScenarioStep,
    SpecError,
)

_OPERATIONS = {
    "prompt",
    "checkpoint",
    "evaluate",
    "compact",
    "restart_pi",
    "fork",
    "switch_branch",
    "budget",
}


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SpecError(f"{where} must be a mapping with string keys")
    return value


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{where} must be a non-empty string")
    return value.strip()


def load_document(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path).expanduser().resolve()
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read {source}: {exc}") from exc
    try:
        if source.suffix.lower() == ".toml":
            value = tomllib.loads(raw_bytes.decode("utf-8"))
        elif source.suffix.lower() in {".yaml", ".yml"}:
            value = yaml.safe_load(raw_bytes) or {}
        else:
            raise SpecError(f"unsupported specification format: {source.suffix or '<none>'}")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot parse {source}: {exc}") from exc
    return _mapping(value, str(source)), content_hash(raw_bytes)


def _resolve_prompt(arguments: dict[str, Any], base: Path, where: str) -> dict[str, Any]:
    has_file = "file" in arguments
    has_text = "text" in arguments
    if has_file == has_text:
        raise SpecError(f"{where} requires exactly one of 'file' or 'text'")
    unknown = set(arguments) - {"file", "text"}
    if unknown:
        raise SpecError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    if has_text:
        return {"text": _nonempty_string(arguments["text"], f"{where}.text")}

    prompt_path = relative_to(base, _nonempty_string(arguments["file"], f"{where}.file"))
    try:
        prompt_bytes = prompt_path.read_bytes()
        prompt_text = prompt_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"cannot read prompt {prompt_path}: {exc}") from exc
    if not prompt_text.strip():
        raise SpecError(f"{where}.file is empty: {prompt_path}")
    return {
        "text": prompt_text,
        "source": {"path": str(prompt_path), "content_hash": content_hash(prompt_bytes)},
    }


def _resolve_step(value: Any, base: Path, index: int) -> ScenarioStep:
    where = f"steps[{index}]"
    step = _mapping(value, where)
    if len(step) != 1:
        raise SpecError(f"{where} must contain exactly one operation")
    operation, raw_arguments = next(iter(step.items()))
    if operation not in _OPERATIONS:
        raise SpecError(f"{where} has unsupported operation {operation!r}")
    if raw_arguments is None:
        arguments: dict[str, Any] = {}
    elif isinstance(raw_arguments, str) and operation in {"checkpoint", "fork", "switch_branch"}:
        arguments = {"name" if operation == "checkpoint" else "entry_id": raw_arguments}
    else:
        arguments = copy.deepcopy(_mapping(raw_arguments, f"{where}.{operation}"))

    if operation == "prompt":
        arguments = _resolve_prompt(arguments, base, f"{where}.prompt")
    elif operation == "checkpoint":
        unknown = set(arguments) - {"name"}
        if unknown:
            raise SpecError(f"{where}.checkpoint has unknown fields: {', '.join(sorted(unknown))}")
        arguments["name"] = _nonempty_string(arguments.get("name"), f"{where}.checkpoint.name")
    elif operation == "evaluate":
        allowed = {"name", "commands", "hidden", "inject_result", "timeout_seconds"}
        unknown = set(arguments) - allowed
        if unknown:
            raise SpecError(f"{where}.evaluate has unknown fields: {', '.join(sorted(unknown))}")
        arguments["name"] = _nonempty_string(arguments.get("name"), f"{where}.evaluate.name")
        commands = arguments.get("commands")
        if isinstance(commands, str):
            commands = [commands]
        if not isinstance(commands, list) or not commands:
            raise SpecError(f"{where}.evaluate.commands must be a non-empty list")
        arguments["commands"] = [
            _nonempty_string(command, f"{where}.evaluate.commands") for command in commands
        ]
        arguments["hidden"] = bool(arguments.get("hidden", False))
        arguments["inject_result"] = bool(arguments.get("inject_result", False))
        if "timeout_seconds" in arguments and (
            not isinstance(arguments["timeout_seconds"], (int, float))
            or arguments["timeout_seconds"] <= 0
        ):
            raise SpecError(f"{where}.evaluate.timeout_seconds must be positive")
    elif operation == "compact":
        unknown = set(arguments) - {"custom_instructions", "customInstructions"}
        if unknown:
            raise SpecError(f"{where}.compact has unknown fields: {', '.join(sorted(unknown))}")
    elif operation == "restart_pi":
        if arguments:
            raise SpecError(f"{where}.restart_pi does not accept fields")
    elif operation == "fork":
        unknown = set(arguments) - {"entry_id", "entryId"}
        if unknown:
            raise SpecError(f"{where}.fork has unknown fields: {', '.join(sorted(unknown))}")
        entry_id = arguments.get("entry_id", arguments.get("entryId"))
        arguments = {"entry_id": _nonempty_string(entry_id, f"{where}.fork.entry_id")}
    elif operation == "switch_branch":
        unknown = set(arguments) - {"entry_id"}
        if unknown:
            raise SpecError(f"{where}.switch_branch has unknown fields: {', '.join(sorted(unknown))}")
        arguments["entry_id"] = _nonempty_string(
            arguments.get("entry_id"), f"{where}.switch_branch.entry_id"
        )
    elif operation == "budget":
        allowed = {"max_cost_usd", "max_wall_seconds", "max_model_turns"}
        unknown = set(arguments) - allowed
        if unknown or not arguments:
            detail = "at least one limit" if not arguments else f"unknown fields: {', '.join(sorted(unknown))}"
            raise SpecError(f"{where}.budget requires {detail}")
        for key, limit in arguments.items():
            if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
                raise SpecError(f"{where}.budget.{key} must be positive")
    return ScenarioStep(operation=operation, arguments=arguments)


def load_scenario(path: str | Path) -> ScenarioSpec:
    source_path = Path(path).expanduser().resolve()
    raw, digest = load_document(source_path)
    base = source_path.parent
    name = _nonempty_string(raw.get("name"), "scenario.name")
    repo = _mapping(raw.get("repo"), "scenario.repo")
    repo_source = relative_to(base, _nonempty_string(repo.get("source"), "scenario.repo.source"))
    revision = _nonempty_string(repo.get("revision"), "scenario.repo.revision")
    unknown_repo = set(repo) - {"source", "revision"}
    if unknown_repo:
        raise SpecError(f"scenario.repo has unknown fields: {', '.join(sorted(unknown_repo))}")
    driver = _nonempty_string(raw.get("driver", "rpc"), "scenario.driver").lower()
    if driver not in {"json", "rpc"}:
        raise SpecError("scenario.driver must be 'json' or 'rpc'")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SpecError("scenario.steps must be a non-empty list")
    steps = tuple(_resolve_step(value, base, index) for index, value in enumerate(raw_steps))
    if driver == "json":
        if sum(step.operation == "prompt" for step in steps) != 1:
            raise SpecError("json scenarios must contain exactly one prompt")
        live_only = {"compact", "restart_pi", "fork", "switch_branch"}
        unsupported = [step.operation for step in steps if step.operation in live_only]
        if unsupported:
            raise SpecError(
                "json scenarios cannot use live-session operation(s): "
                + ", ".join(unsupported)
            )
        if any(
            step.operation == "evaluate" and step.arguments.get("inject_result")
            for step in steps
        ):
            raise SpecError("json scenarios cannot inject evaluator results into the agent")
    return ScenarioSpec(
        name=name,
        repo=RepositorySpec(source=repo_source, revision=revision),
        driver=driver,
        steps=steps,
        source_path=source_path,
        content_hash=digest,
    )


def _resolve_configuration_paths(value: Any, base: Path) -> Any:
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    resolved = copy.deepcopy(value)
    for field in ("extensions", "skills", "prompt_templates", "append_system_prompts"):
        resources = resolved.get(field)
        if not isinstance(resources, list):
            continue
        for index, resource in enumerate(resources):
            if isinstance(resource, str):
                if resource.startswith(("./", "../", "~/", "/")):
                    resources[index] = str(relative_to(base, resource))
            elif isinstance(resource, dict) and isinstance(resource.get("source"), str):
                source = resource["source"]
                if source.startswith(("./", "../", "~/", "/")):
                    resource["source"] = str(relative_to(base, source))
    return resolved


def load_experiment(path: str | Path) -> ExperimentSpec:
    source_path = Path(path).expanduser().resolve()
    raw, digest = load_document(source_path)
    base = source_path.parent
    name = _nonempty_string(raw.get("name"), "experiment.name")
    scenario_path = relative_to(
        base, _nonempty_string(raw.get("scenario"), "experiment.scenario")
    )
    repetitions = raw.get("repetitions", 1)
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        raise SpecError("experiment.repetitions must be a positive integer")
    raw_matrix = _mapping(raw.get("matrix"), "experiment.matrix")
    if not raw_matrix:
        raise SpecError("experiment.matrix must define at least one axis")
    matrix: dict[str, tuple[Any, ...]] = {}
    for axis, values in raw_matrix.items():
        if not isinstance(values, list) or not values:
            raise SpecError(f"experiment.matrix.{axis} must be a non-empty list")
        matrix[axis] = tuple(
            _resolve_configuration_paths(value, base) if axis == "configuration" else copy.deepcopy(value)
            for value in values
        )

    run_raw = _mapping(raw.get("run", {}), "experiment.run")
    allowed_run = {
        "isolation",
        "timeout_seconds",
        "max_parallel_per_provider",
        "results_dir",
        "pi_executable",
        "credential_source",
        "host_pi_state_dir",
    }
    unknown_run = set(run_raw) - allowed_run
    if unknown_run:
        raise SpecError(f"experiment.run has unknown fields: {', '.join(sorted(unknown_run))}")
    isolation = str(run_raw.get("isolation", "fast"))
    if isolation not in {"fast", "strict"}:
        raise SpecError("experiment.run.isolation must be 'fast' or 'strict'")
    timeout = run_raw.get("timeout_seconds", 3600)
    parallel = run_raw.get("max_parallel_per_provider", 1)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise SpecError("experiment.run.timeout_seconds must be positive")
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise SpecError("experiment.run.max_parallel_per_provider must be a positive integer")
    credential_source = str(run_raw.get("credential_source", "environment"))
    if credential_source not in {"environment", "host_auth", "none"}:
        raise SpecError(
            "experiment.run.credential_source must be 'environment', 'host_auth', or 'none'"
        )
    host_state = run_raw.get("host_pi_state_dir")
    if credential_source == "host_auth" and not host_state:
        raise SpecError("experiment.run.host_pi_state_dir is required for host_auth")
    pi_executable = _nonempty_string(
        run_raw.get("pi_executable", "pi"), "experiment.run.pi_executable"
    )
    if pi_executable.startswith(("./", "../", "~/", "/")):
        pi_executable = str(relative_to(base, pi_executable))
    options = RunOptions(
        isolation=isolation,
        timeout_seconds=float(timeout),
        max_parallel_per_provider=parallel,
        results_dir=relative_to(base, run_raw.get("results_dir", "results")),
        pi_executable=pi_executable,
        credential_source=credential_source,
        host_pi_state_dir=relative_to(base, host_state) if host_state else None,
    )
    return ExperimentSpec(
        name=name,
        source_path=source_path,
        scenario_path=scenario_path,
        repetitions=repetitions,
        matrix=matrix,
        run=options,
        raw=raw,
        content_hash=digest,
    )
