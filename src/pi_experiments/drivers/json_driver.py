"""One-shot Pi JSON driver."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from ..spec import RunSpec
from ..workspace.base import Workspace
from .base import (
    DriverCheckpoint,
    DriverError,
    EventSink,
    StderrSink,
    TurnResult,
    UnsupportedDriverOperation,
)
from .config import process_arguments, process_environment


class JsonDriver:
    def __init__(
        self,
        run: RunSpec,
        workspace: Workspace,
        event_sink: EventSink,
        stderr_sink: StderrSink,
    ):
        self.run = run
        self.workspace = workspace
        self.event_sink = event_sink
        self.stderr_sink = stderr_sink
        self.session_file: Path | None = None
        self.returncode: int | None = None
        self._used = False

    def start(self) -> None:
        return None

    def prompt(self, text: str, *, timeout: float | None = None) -> TurnResult:
        if self._used:
            raise UnsupportedDriverOperation("the JSON driver supports exactly one prompt")
        self._used = True
        arguments = process_arguments(self.run, self.workspace, mode="json")
        arguments.extend(["--print", "--name", self.run.run_id, text])
        started = time.monotonic()
        try:
            process = subprocess.run(
                arguments,
                cwd=self.workspace.repository,
                env=process_environment(self.run, self.workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if exc.stderr:
                self.stderr_sink(exc.stderr)
            raise DriverError(f"Pi JSON prompt timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise DriverError(f"cannot start Pi: {exc}") from exc
        self.returncode = process.returncode
        if process.stderr:
            self.stderr_sink(process.stderr)
        events: list[Mapping[str, Any]] = []
        for raw_line in process.stdout.split(b"\n"):
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
                if not isinstance(event, dict):
                    raise ValueError("JSON event is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                event = {
                    "type": "harness_protocol_error",
                    "driver": "json",
                    "error": str(exc),
                    "raw": raw_line.decode("utf-8", errors="replace"),
                }
            events.append(event)
            self.event_sink(event)
        self.session_file = _latest_session(self.workspace.sessions)
        response = _last_assistant_text(events)
        usage = _last_usage(events)
        if process.returncode != 0:
            raise DriverError(f"Pi JSON process exited with status {process.returncode}")
        return TurnResult(
            response_text=response,
            messages=tuple(events),
            usage=usage,
            elapsed_seconds=time.monotonic() - started,
        )

    def compact(self, arguments: Mapping[str, Any], *, timeout: float | None = None) -> Mapping[str, Any]:
        raise UnsupportedDriverOperation("compact requires the RPC driver")

    def restart(self, *, timeout: float | None = None) -> None:
        raise UnsupportedDriverOperation("restart_pi requires the RPC driver")

    def checkpoint(self, *, timeout: float | None = None) -> DriverCheckpoint:
        raise UnsupportedDriverOperation("live checkpoints require the RPC driver")

    def fork(self, entry_id: str, *, timeout: float | None = None) -> Mapping[str, Any]:
        raise UnsupportedDriverOperation("fork requires the RPC driver")

    def stop(self) -> None:
        self.session_file = self.session_file or _latest_session(self.workspace.sessions)


def _latest_session(directory: Path) -> Path | None:
    candidates = list(directory.rglob("*.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def _message_text(message: Any) -> str:
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _last_assistant_text(events: list[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        text = _message_text(event.get("message"))
        if text:
            return text
        messages = event.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                text = _message_text(message)
                if text:
                    return text
    return ""


def _last_usage(events: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    for event in reversed(events):
        message = event.get("message")
        if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping):
            return message["usage"]
    return {}
