"""Immutable specification models used by the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class SpecError(ValueError):
    """A human-authored experiment or scenario specification is invalid."""


@dataclass(frozen=True)
class RepositorySpec:
    source: Path
    revision: str


@dataclass(frozen=True)
class ScenarioStep:
    """One ordered scenario operation with its fully resolved arguments."""

    operation: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    repo: RepositorySpec
    driver: str
    steps: tuple[ScenarioStep, ...]
    source_path: Path
    content_hash: str


@dataclass(frozen=True)
class RunOptions:
    isolation: str = "fast"
    timeout_seconds: float = 3600.0
    max_parallel_per_provider: int = 1
    results_dir: Path = Path("results")
    pi_executable: str = "pi"
    credential_source: str = "environment"
    host_pi_state_dir: Path | None = None


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    source_path: Path
    scenario_path: Path
    repetitions: int
    matrix: Mapping[str, tuple[Any, ...]]
    run: RunOptions
    raw: Mapping[str, Any]
    content_hash: str


@dataclass(frozen=True)
class VariantSpec:
    id: str
    name: str
    axes: Mapping[str, Any]
    fingerprint: str

    @property
    def model(self) -> Mapping[str, Any]:
        value = self.axes.get("model", {})
        return value if isinstance(value, Mapping) else {"id": value}

    @property
    def configuration(self) -> Mapping[str, Any]:
        value = self.axes.get("configuration", {})
        return value if isinstance(value, Mapping) else {"name": value}


@dataclass(frozen=True)
class RunSpec:
    """A resolved run. This object is serialized before Pi starts."""

    schema_version: int
    experiment_id: str
    run_id: str
    invocation_id: str
    repetition: int
    spec_fingerprint: str
    experiment_name: str
    variant: VariantSpec
    scenario: ScenarioSpec
    options: RunOptions
    resolved_revision: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
