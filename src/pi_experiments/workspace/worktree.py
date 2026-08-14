"""Fast isolation using a fresh detached Git worktree."""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..spec import RunSpec
from .base import Workspace


class WorkspaceError(RuntimeError):
    """A disposable workspace could not be prepared or removed."""


def _git(source: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(source), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = f": {exc.stderr.decode('utf-8', errors='replace').strip()}"
        raise WorkspaceError(f"git {' '.join(arguments)} failed{detail}") from exc


def _text(process: subprocess.CompletedProcess[bytes]) -> str:
    return process.stdout.decode("utf-8", errors="replace").strip()


class FastWorktreeBackend:
    """Creates a detached worktree and isolated Pi state under a temporary root."""

    def __init__(self, temporary_root: str | Path | None = None):
        self.temporary_root = (
            Path(temporary_root).expanduser().resolve() if temporary_root is not None else None
        )

    def prepare(self, run: RunSpec) -> Workspace:
        if run.options.isolation != "fast":
            raise NotImplementedError(
                "strict isolation is declared but not implemented; use isolation: fast"
            )
        source = run.scenario.repo.source.resolve()
        if not source.is_dir():
            raise WorkspaceError(f"repository does not exist: {source}")
        requested = run.scenario.repo.revision
        baseline = _text(_git(source, "rev-parse", "--verify", f"{requested}^{{commit}}"))
        canonical = _text(_git(source, "rev-parse", "--show-toplevel"))
        source_dirty = _text(_git(source, "status", "--porcelain=v2", "--untracked-files=all"))
        submodules = _text(_git(source, "submodule", "status", "--recursive", check=False))
        root = Path(
            tempfile.mkdtemp(
                prefix=f"piexp-{run.run_id[:32]}-",
                dir=str(self.temporary_root) if self.temporary_root else None,
            )
        ).resolve()
        repository = root / "repo"
        worktree_added = False
        try:
            _git(source, "worktree", "add", "--detach", str(repository), baseline)
            worktree_added = True
            pi_state = root / "pi-state"
            sessions = root / "sessions"
            pi_state.mkdir(mode=0o700)
            sessions.mkdir(mode=0o700)
        except Exception:
            if worktree_added:
                subprocess.run(
                    ["git", "-C", str(source), "worktree", "remove", "--force", str(repository)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            shutil.rmtree(root, ignore_errors=True)
            raise
        provenance: dict[str, Any] = {
            "repository": {
                "canonical_path": canonical,
                "requested_revision": requested,
                "resolved_sha": baseline,
                "source_dirty_porcelain_v2": source_dirty,
                "submodules": submodules.splitlines() if submodules else [],
            },
            "environment": {
                "isolation_backend": "fast-worktree",
                "platform": platform.platform(),
                "python": platform.python_version(),
                "shared_cache_policy": "host caches may be visible",
                "network_policy": "host network",
            },
        }
        return Workspace(
            root=root,
            repository=repository,
            pi_state=pi_state,
            sessions=sessions,
            baseline_sha=baseline,
            provenance=provenance,
        )

    def cleanup(self, workspace: Workspace) -> None:
        source_value = workspace.provenance.get("repository", {}).get("canonical_path")
        source = Path(str(source_value)) if source_value else None
        errors: list[str] = []
        if source is not None and source.exists() and workspace.repository.exists():
            process = subprocess.run(
                ["git", "-C", str(source), "worktree", "remove", "--force", str(workspace.repository)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.returncode != 0:
                errors.append(process.stderr.decode("utf-8", errors="replace").strip())
        shutil.rmtree(workspace.root, ignore_errors=True)
        if source is not None and source.exists():
            subprocess.run(
                ["git", "-C", str(source), "worktree", "prune"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if errors:
            raise WorkspaceError("worktree cleanup failed: " + "; ".join(errors))
