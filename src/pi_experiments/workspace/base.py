"""Workspace backend contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..spec import RunSpec


@dataclass(frozen=True)
class Workspace:
    """One disposable run workspace."""

    root: Path
    repository: Path
    pi_state: Path
    sessions: Path
    baseline_sha: str
    provenance: Mapping[str, Any]


class WorkspaceBackend(Protocol):
    def prepare(self, run: RunSpec) -> Workspace: ...

    def cleanup(self, workspace: Workspace) -> None: ...
