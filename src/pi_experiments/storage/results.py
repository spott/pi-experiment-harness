"""Filesystem result bundles. Raw files here are the source of truth."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..paths import safe_component
from ..serde import to_primitive
from ..spec import ExperimentSpec, RunSpec, ScenarioSpec


@dataclass(frozen=True)
class RunPaths:
    root: Path
    run_spec: Path
    events: Path
    session: Path
    stderr: Path
    turns: Path
    checkpoints: Path
    final_response: Path
    metrics: Path
    evaluator: Path


class ResultStore:
    """Creates append-only experiment bundles and atomically writes artifacts."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def experiment_dir(self, experiment_id: str) -> Path:
        return self.root / safe_component(experiment_id, label="experiment id")

    def run_dir(self, experiment_id: str, run_id: str) -> Path:
        return self.experiment_dir(experiment_id) / "runs" / safe_component(
            run_id, label="run id"
        )

    def initialize_experiment(
        self, experiment_id: str, experiment: ExperimentSpec, scenario: ScenarioSpec
    ) -> Path:
        directory = self.experiment_dir(experiment_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "runs").mkdir(exist_ok=True)
        self._copy_once(experiment.source_path, directory / f"experiment{experiment.source_path.suffix}")
        self._copy_once(scenario.source_path, directory / f"scenario{scenario.source_path.suffix}")
        self._write_json_once(
            directory / "experiment-meta.json",
            {
                "experiment_id": experiment_id,
                "experiment_content_hash": experiment.content_hash,
                "scenario_content_hash": scenario.content_hash,
            },
            reuse_identical=True,
        )
        return directory

    def create_run(self, run: RunSpec) -> RunPaths:
        directory = self.run_dir(run.experiment_id, run.run_id)
        try:
            directory.mkdir(parents=False)
        except FileExistsError as exc:
            raise FileExistsError(f"run result already exists: {directory}") from exc
        (directory / "turns").mkdir()
        (directory / "checkpoints").mkdir()
        paths = self.paths(run.experiment_id, run.run_id)
        self._write_json_once(paths.run_spec, run)
        return paths

    def paths(self, experiment_id: str, run_id: str) -> RunPaths:
        root = self.run_dir(experiment_id, run_id)
        return RunPaths(
            root=root,
            run_spec=root / "run-spec.json",
            events=root / "events.jsonl",
            session=root / "session.jsonl",
            stderr=root / "stderr.log",
            turns=root / "turns",
            checkpoints=root / "checkpoints",
            final_response=root / "final-response.md",
            metrics=root / "metrics.json",
            evaluator=root / "evaluator.json",
        )

    def write_json(self, run: RunSpec, relative_path: str | Path, value: Any) -> Path:
        destination = self._artifact_path(run, relative_path)
        self._write_json_once(destination, value)
        return destination

    def write_bytes(
        self, run: RunSpec, relative_path: str | Path, data: bytes, *, overwrite: bool = False
    ) -> Path:
        destination = self._artifact_path(run, relative_path)
        self._atomic_write(destination, data, overwrite=overwrite)
        return destination

    def append_jsonl(self, run: RunSpec, relative_path: str | Path, values: Iterable[Any]) -> Path:
        destination = self._artifact_path(run, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("ab") as handle:
            for value in values:
                encoded = json.dumps(
                    to_primitive(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return destination

    def append_bytes(self, run: RunSpec, relative_path: str | Path, data: bytes) -> Path:
        destination = self._artifact_path(run, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return destination

    def finalize(self, run: RunSpec, summary: Any) -> Path:
        return self.write_json(run, "completed.json", summary)

    def is_finalized(self, experiment_id: str, run_id: str) -> bool:
        return (self.run_dir(experiment_id, run_id) / "completed.json").is_file()

    def _artifact_path(self, run: RunSpec, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"artifact path must stay inside its run: {relative_path}")
        root = self.run_dir(run.experiment_id, run.run_id)
        destination = root / relative
        if (root / "completed.json").exists():
            raise RuntimeError(f"run is finalized and immutable: {run.run_id}")
        return destination

    @staticmethod
    def _copy_once(source: Path, destination: Path) -> None:
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise FileExistsError(f"artifact differs from existing file: {destination}")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_handle:
            data = input_handle.read()
        ResultStore._atomic_write(destination, data)

    @staticmethod
    def _write_json_once(
        destination: Path, value: Any, *, reuse_identical: bool = False
    ) -> None:
        data = (
            json.dumps(to_primitive(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        try:
            ResultStore._atomic_write(destination, data)
        except FileExistsError:
            if reuse_identical and destination.read_bytes() == data:
                return
            if reuse_identical:
                raise FileExistsError(
                    f"artifact differs from existing file: {destination}"
                ) from None
            raise

    @staticmethod
    def _atomic_write(destination: Path, data: bytes, *, overwrite: bool = False) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"artifact already exists: {destination}")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not overwrite and destination.exists():
                raise FileExistsError(f"artifact already exists: {destination}")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
