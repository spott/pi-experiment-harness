"""Command-line workflows for running and inspecting experiments."""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .execution import RunExecutor
from .matrix import expand_runs, expand_variants
from .scenario import load_experiment, load_scenario
from .spec import ExperimentSpec, VariantSpec
from .storage import ResultIndex, ResultStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piexp", description="Reproducible experiment harness for Pi coding agent"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="expand and execute an experiment")
    run.add_argument("experiment", type=Path)
    run.add_argument(
        "--variant",
        action="append",
        default=[],
        help="variant id, full variant name, or configuration name (repeatable)",
    )
    run.add_argument("--repeat", type=int, help="override repetitions per variant")
    run.add_argument("--invocation-id", help="stable invocation label (run IDs remain unique to it)")
    run.add_argument("--results-dir", type=Path, help="override the experiment results directory")

    show = subcommands.add_parser("show", help="show one indexed run and its metrics")
    show.add_argument("run_id")
    show.add_argument("--results-dir", type=Path, default=Path("results"))

    compare = subcommands.add_parser("compare", help="summarize variants in an experiment")
    compare.add_argument("experiment_id")
    compare.add_argument("--results-dir", type=Path, default=Path("results"))

    gc = subcommands.add_parser("gc", help="find stale disposable piexp workspaces")
    gc.add_argument("--temporary-root", type=Path, default=Path(tempfile.gettempdir()))
    gc.add_argument("--older-than-hours", type=float, default=24.0)
    gc.add_argument("--apply", action="store_true", help="remove candidates; default is dry-run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            value = run_experiment(
                args.experiment,
                variants=args.variant,
                repetitions=args.repeat,
                invocation_id=args.invocation_id,
                results_dir=args.results_dir,
            )
        elif args.command == "show":
            value = show_run(args.results_dir, args.run_id)
        elif args.command == "compare":
            value = compare_experiment(args.results_dir, args.experiment_id)
        else:
            value = collect_garbage(
                args.temporary_root,
                older_than_hours=args.older_than_hours,
                apply=args.apply,
            )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"piexp: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def run_experiment(
    experiment_path: str | Path,
    *,
    variants: Sequence[str] = (),
    repetitions: int | None = None,
    invocation_id: str | None = None,
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    experiment = load_experiment(experiment_path)
    if results_dir is not None:
        options = dataclasses.replace(experiment.run, results_dir=Path(results_dir).expanduser().resolve())
        experiment = dataclasses.replace(experiment, run=options)
    selected_ids = _select_variants(expand_variants(experiment.matrix), variants)
    runs = expand_runs(
        experiment,
        invocation_id=invocation_id,
        repetitions=repetitions,
        variant_ids=selected_ids,
    )
    if not runs:
        raise ValueError("experiment expansion selected no runs")
    scenario = runs[0].scenario
    store = ResultStore(experiment.run.results_dir)
    index = ResultIndex(experiment.run.results_dir / "index.sqlite3")
    store.initialize_experiment(runs[0].experiment_id, experiment, scenario)
    index.register_experiment(runs[0].experiment_id, experiment, scenario)
    for variant in {run.variant.id: run.variant for run in runs}.values():
        index.register_variant(runs[0].experiment_id, variant)
    executor = RunExecutor(store, index)
    results = [executor.execute(run) for run in runs]
    return {
        "experiment_id": runs[0].experiment_id,
        "invocation_id": runs[0].invocation_id,
        "results_dir": str(experiment.run.results_dir),
        "run_count": len(results),
        "statuses": dict(sorted(Counter(result.status for result in results).items())),
        "runs": [
            {
                "run_id": result.run.run_id,
                "variant_id": result.run.variant.id,
                "repetition": result.run.repetition,
                "status": result.status,
                "exit_reason": result.exit_reason,
                "result_path": str(result.result_path),
                "error": result.error,
            }
            for result in results
        ],
    }


def _select_variants(
    variants: Sequence[VariantSpec], selectors: Sequence[str]
) -> list[str] | None:
    if not selectors:
        return None
    selected: set[str] = set()
    for selector in selectors:
        matches = []
        for variant in variants:
            configuration = variant.configuration
            configuration_name = configuration.get("name")
            if selector in {variant.id, variant.name, configuration_name}:
                matches.append(variant.id)
        if not matches:
            raise ValueError(f"unknown variant selector: {selector}")
        selected.update(matches)
    return sorted(selected)


def _open_index(results_dir: str | Path) -> ResultIndex:
    path = Path(results_dir).expanduser().resolve() / "index.sqlite3"
    if not path.is_file():
        raise ValueError(f"result index does not exist: {path}")
    return ResultIndex(path)


def show_run(results_dir: str | Path, run_id: str) -> dict[str, Any]:
    index = _open_index(results_dir)
    run = index.find_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    return {
        "run": run,
        "metrics": index.get_metrics(run["id"]),
        "evaluations": index.get_evaluations(run["id"]),
        "artifacts": index.get_artifacts(run["id"]),
    }


def compare_experiment(results_dir: str | Path, experiment_id: str) -> dict[str, Any]:
    index = _open_index(results_dir)
    experiments = index.list_experiments()
    matches = [item for item in experiments if item["id"] == experiment_id]
    if not matches:
        matches = [item for item in experiments if item["id"].startswith(experiment_id)]
    if len(matches) > 1:
        raise ValueError(f"experiment id prefix is ambiguous: {experiment_id}")
    if not matches:
        raise ValueError(f"experiment not found: {experiment_id}")
    resolved_id = matches[0]["id"]
    runs = index.list_runs(resolved_id)
    variants = {item["id"]: item for item in index.list_variants(resolved_id)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault(run["variant_id"], []).append(run)
    compared: list[dict[str, Any]] = []
    for variant_id, variant_runs in sorted(groups.items()):
        metrics = [index.get_metrics(run["id"]) for run in variant_runs]
        scalar_values: dict[str, list[float]] = {}
        for run_metrics in metrics:
            for namespace, values in run_metrics.items():
                for key, value in values.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        scalar_values.setdefault(f"{namespace}.{key}", []).append(float(value))
        compared.append(
            {
                "variant": variants.get(variant_id, {"id": variant_id}),
                "run_count": len(variant_runs),
                "statuses": dict(sorted(Counter(run["status"] for run in variant_runs).items())),
                "metrics": {
                    key: _distribution(values) for key, values in sorted(scalar_values.items())
                },
            }
        )
    return {
        "experiment": matches[0],
        "run_count": len(runs),
        "variants": compared,
    }


def _distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def collect_garbage(
    temporary_root: str | Path,
    *,
    older_than_hours: float = 24.0,
    apply: bool = False,
) -> dict[str, Any]:
    if older_than_hours < 0:
        raise ValueError("older-than-hours must not be negative")
    root = Path(temporary_root).expanduser().resolve()
    cutoff = time.time() - older_than_hours * 3600
    candidates = [
        path
        for path in sorted(root.glob("piexp-*"))
        if path.is_dir() and path.stat().st_mtime <= cutoff
    ]
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    if apply:
        for path in candidates:
            repository = path / "repo"
            try:
                if repository.is_dir():
                    subprocess.run(
                        ["git", "-C", str(repository), "worktree", "remove", "--force", str(repository)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                shutil.rmtree(path)
                removed.append(str(path))
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
    return {
        "temporary_root": str(root),
        "older_than_hours": older_than_hours,
        "dry_run": not apply,
        "candidate_count": len(candidates),
        "candidates": [str(path) for path in candidates],
        "removed": removed,
        "errors": errors,
    }


if __name__ == "__main__":
    raise SystemExit(main())
