"""Capture final Git state from a disposable worktree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class GitCaptureError(RuntimeError):
    pass


def _run(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise GitCaptureError(f"git {' '.join(arguments)} failed: {detail}")
    return process


def capture_git(repository: Path, baseline_sha: str) -> dict[str, bytes]:
    """Stage the disposable index, then capture a binary patch including new files."""

    status = _run(repository, "status", "--porcelain=v2", "--untracked-files=all").stdout
    _run(repository, "add", "-A", "--", ".")
    patch = _run(repository, "diff", "--cached", "--binary", baseline_sha, "--").stdout
    stat = _run(repository, "diff", "--cached", "--stat", baseline_sha, "--").stdout
    name_status = _run(
        repository, "diff", "--cached", "--name-status", "-z", baseline_sha, "--"
    ).stdout
    tree = _run(repository, "write-tree").stdout.decode("ascii").strip()
    records = _parse_name_status(name_status)
    return {
        "baseline.txt": (baseline_sha + "\n").encode(),
        "git-status.txt": status,
        "patch.diff": patch,
        "patch-stat.txt": stat,
        "changed-files.json": (json.dumps(records, indent=2, sort_keys=True) + "\n").encode(),
        "final-tree.txt": (tree + "\n").encode(),
    }


def checkpoint_git(repository: Path, baseline_sha: str) -> dict[str, Any]:
    status = _run(repository, "status", "--porcelain=v2", "--untracked-files=all").stdout
    diff = _run(repository, "diff", "--stat", baseline_sha, "--").stdout
    return {
        "baseline_sha": baseline_sha,
        "head_sha": _run(repository, "rev-parse", "HEAD").stdout.decode("ascii").strip(),
        "status_porcelain_v2": status.decode("utf-8", errors="replace"),
        "diff_stat": diff.decode("utf-8", errors="replace"),
    }


def _parse_name_status(value: bytes) -> list[dict[str, str]]:
    parts = value.decode("utf-8", errors="surrogateescape").split("\0")
    if parts and not parts[-1]:
        parts.pop()
    result: list[dict[str, str]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if status.startswith(("R", "C")) and index + 1 < len(parts):
            result.append({"status": status, "old_path": parts[index], "path": parts[index + 1]})
            index += 2
        elif index < len(parts):
            result.append({"status": status, "path": parts[index]})
            index += 1
    return result
