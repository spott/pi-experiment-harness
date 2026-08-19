"""Capture private task-framework worker runs referenced by a Pi session."""

from __future__ import annotations

import json
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

_RUN_CONTEXT_TYPE = "pi-task-framework/run-context"
_RUN_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
_MAX_CAPTURE_BYTES = 512 * 1024 * 1024


class TaskFrameworkCaptureError(RuntimeError):
    """A task-framework run reference or artifact tree is unsafe or malformed."""


def capture_task_framework_runs(session_file: str | Path) -> dict[str, bytes]:
    """Return immutable artifact bytes for every run referenced by the session.

    Production task-framework run directories are constrained to the extension's
    private temporary root. Symlinks and non-regular nodes are rejected rather
    than followed.
    """

    source = Path(session_file)
    if not source.is_file():
        return {}
    records = _run_contexts(source)
    captured: dict[str, bytes] = {}
    total = 0
    for run_id, directory_text in sorted(records.items()):
        if _RUN_ID.fullmatch(run_id) is None:
            raise TaskFrameworkCaptureError(f"unsafe task-framework run ID: {run_id!r}")
        private_root = (Path(tempfile.gettempdir()) / "pi-task-framework-runs").resolve()
        expected = private_root / run_id
        directory = Path(directory_text).expanduser().absolute()
        if directory != expected:
            raise TaskFrameworkCaptureError(
                f"task-framework run directory is outside its private root: {directory}"
            )
        _assert_real_directory(private_root)
        _assert_no_symlink_chain(private_root, Path(run_id))
        _assert_real_directory(directory)
        metadata = _read_json(directory / "run.json")
        if metadata.get("schemaVersion") != 1 or metadata.get("runId") != run_id:
            raise TaskFrameworkCaptureError(
                f"task-framework run metadata does not match {run_id}"
            )
        for item in sorted(directory.rglob("*")):
            relative = item.relative_to(directory)
            _assert_no_symlink_chain(directory, relative)
            if item.is_dir():
                continue
            if not item.is_file():
                raise TaskFrameworkCaptureError(
                    f"task-framework artifact is not a regular file: {item}"
                )
            data = item.read_bytes()
            total += len(data)
            if total > _MAX_CAPTURE_BYTES:
                raise TaskFrameworkCaptureError(
                    f"task-framework artifacts exceed {_MAX_CAPTURE_BYTES} bytes"
                )
            destination = (Path("task-framework-runs") / run_id / relative).as_posix()
            if destination in captured:
                raise TaskFrameworkCaptureError(
                    f"duplicate task-framework artifact destination: {destination}"
                )
            captured[destination] = data
    return captured


def _run_contexts(session_file: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line_number, raw in enumerate(session_file.read_bytes().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, Mapping) or entry.get("customType") != _RUN_CONTEXT_TYPE:
            continue
        data = entry.get("data")
        if not isinstance(data, Mapping):
            raise TaskFrameworkCaptureError(
                f"malformed task-framework run context at session line {line_number}"
            )
        run_id = data.get("runId")
        directory = data.get("directory")
        if data.get("schemaVersion") != 1 or not isinstance(run_id, str) or not isinstance(directory, str):
            raise TaskFrameworkCaptureError(
                f"malformed task-framework run context at session line {line_number}"
            )
        existing = records.get(run_id)
        if existing is not None and existing != directory:
            raise TaskFrameworkCaptureError(
                f"task-framework run {run_id} has conflicting directories"
            )
        records[run_id] = directory
    return records


def _assert_real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TaskFrameworkCaptureError(f"cannot inspect task-framework run directory {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskFrameworkCaptureError(
            f"task-framework run path is not a real directory: {path}"
        )


def _assert_no_symlink_chain(root: Path, relative: Path) -> None:
    current = root
    for component in relative.parts:
        current /= component
        try:
            if current.is_symlink():
                raise TaskFrameworkCaptureError(
                    f"task-framework artifact tree contains a symlink: {current}"
                )
        except OSError as exc:
            raise TaskFrameworkCaptureError(
                f"cannot inspect task-framework artifact {current}: {exc}"
            ) from exc


def _read_json(path: Path) -> Mapping[str, Any]:
    _assert_no_symlink_chain(path.parent, Path(path.name))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrameworkCaptureError(f"cannot read task-framework metadata {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise TaskFrameworkCaptureError(f"task-framework metadata is not an object: {path}")
    return value
