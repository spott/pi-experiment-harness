"""Long-lived Pi RPC subprocess driver."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..spec import RunSpec
from ..workspace.base import Workspace
from .base import DriverCheckpoint, DriverError, EventSink, StderrSink, TurnResult
from .config import process_arguments, process_environment
from .json_driver import _last_assistant_text

_EOF = object()


class RpcDriver:
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
        self._process: subprocess.Popen[bytes] | None = None
        self._inbox: queue.Queue[Mapping[str, Any] | object] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._resume_session: Path | None = None

    def start(self) -> None:
        if self._process is not None:
            raise DriverError("Pi RPC process is already running")
        arguments = process_arguments(self.run, self.workspace, mode="rpc")
        arguments.extend(["--name", self.run.run_id])
        if self._resume_session is not None:
            arguments.extend(["--session", str(self._resume_session)])
        try:
            self._process = subprocess.Popen(
                arguments,
                cwd=self.workspace.repository,
                env=process_environment(self.run, self.workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise DriverError(f"cannot start Pi RPC process: {exc}") from exc
        self._inbox = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def prompt(self, text: str, *, timeout: float | None = None) -> TurnResult:
        started = time.monotonic()
        deadline = _deadline(timeout)
        events: list[Mapping[str, Any]] = []
        self._request("prompt", {"message": text}, deadline=deadline, captured=events)
        if not any(event.get("type") == "agent_settled" for event in events):
            events.extend(self._wait_for_event("agent_settled", deadline=deadline))
        stats = self._request("get_session_stats", deadline=deadline)
        state = self._request("get_state", deadline=deadline)
        self._set_session_file(state)
        return TurnResult(
            response_text=_last_assistant_text(events),
            messages=tuple(events),
            usage=stats,
            elapsed_seconds=time.monotonic() - started,
        )

    def compact(
        self, arguments: Mapping[str, Any], *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {}
        custom = arguments.get("custom_instructions", arguments.get("customInstructions"))
        if custom is not None:
            payload["customInstructions"] = str(custom)
        return self._request("compact", payload, deadline=_deadline(timeout))

    def restart(self, *, timeout: float | None = None) -> None:
        state = self._request("get_state", deadline=_deadline(timeout))
        self._set_session_file(state)
        if self.session_file is None:
            raise DriverError("Pi did not report a session file before restart")
        self._resume_session = self.session_file
        self.stop()
        self.start()
        resumed = self._request("get_state", deadline=_deadline(timeout))
        self._set_session_file(resumed)

    def checkpoint(self, *, timeout: float | None = None) -> DriverCheckpoint:
        deadline = _deadline(timeout)
        state = self._request("get_state", deadline=deadline)
        self._set_session_file(state)
        return DriverCheckpoint(
            state=state,
            stats=self._request("get_session_stats", deadline=deadline),
            entries=self._request("get_entries", deadline=deadline),
            tree=self._request("get_tree", deadline=deadline),
        )

    def fork(self, entry_id: str, *, timeout: float | None = None) -> Mapping[str, Any]:
        return self._request("fork", {"entryId": entry_id}, deadline=_deadline(timeout))

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.returncode = process.returncode
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=2)
        self._process = None

    def _request(
        self,
        command: str,
        data: Mapping[str, Any] | None = None,
        *,
        deadline: float | None,
        captured: list[Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            status = process.returncode if process is not None else None
            raise DriverError(f"Pi RPC process is not running (exit status {status})")
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "type": command, **(dict(data) if data else {})}
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DriverError(f"cannot send RPC command {command}: {exc}") from exc
        while True:
            message = self._next_message(deadline)
            if message.get("type") != "response" or message.get("id") != request_id:
                if captured is not None:
                    captured.append(message)
                continue
            if not message.get("success", False):
                error = message.get("error", "unknown RPC error")
                raise DriverError(f"Pi RPC command {command} failed: {error}")
            value = message.get("data", {})
            return value if isinstance(value, Mapping) else {"value": value}

    def _wait_for_event(
        self, event_type: str, *, deadline: float | None
    ) -> list[Mapping[str, Any]]:
        events: list[Mapping[str, Any]] = []
        while True:
            message = self._next_message(deadline)
            events.append(message)
            if message.get("type") == event_type:
                return events

    def _next_message(self, deadline: float | None) -> Mapping[str, Any]:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            value = self._inbox.get(timeout=remaining)
        except queue.Empty as exc:
            raise DriverError("timed out waiting for Pi RPC output") from exc
        if value is _EOF:
            process = self._process
            status = process.poll() if process is not None else self.returncode
            raise DriverError(f"Pi RPC stdout closed unexpectedly (exit status {status})")
        assert isinstance(value, Mapping)
        return value

    def _set_session_file(self, state: Mapping[str, Any]) -> None:
        value = state.get("sessionFile")
        if isinstance(value, str) and value:
            self.session_file = Path(value).resolve()

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        buffer = b""
        try:
            while True:
                chunk = os.read(self._process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    if raw:
                        self._publish(raw)
            if buffer:
                self._publish(buffer, incomplete=True)
        finally:
            self._inbox.put(_EOF)

    def _publish(self, raw: bytes, *, incomplete: bool = False) -> None:
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("RPC record is not an object")
            message: Mapping[str, Any] = value
        except (json.JSONDecodeError, ValueError) as exc:
            message = {
                "type": "harness_protocol_error",
                "driver": "rpc",
                "error": str(exc),
                "incomplete": incomplete,
                "raw": raw.decode("utf-8", errors="replace"),
            }
        self.event_sink(message)
        self._inbox.put(message)

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            chunk = os.read(self._process.stderr.fileno(), 65536)
            if not chunk:
                return
            self.stderr_sink(chunk)


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + timeout
