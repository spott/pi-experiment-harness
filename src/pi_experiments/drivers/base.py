"""Normalized Pi driver interface and result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..spec import RunSpec
from ..workspace.base import Workspace

EventSink = Callable[[Mapping[str, Any]], None]
StderrSink = Callable[[bytes], None]


class DriverError(RuntimeError):
    """Pi failed or violated the selected driver protocol."""


class UnsupportedDriverOperation(DriverError):
    pass


@dataclass(frozen=True)
class TurnResult:
    response_text: str
    messages: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class DriverCheckpoint:
    state: Mapping[str, Any]
    stats: Mapping[str, Any]
    entries: Mapping[str, Any]
    tree: Mapping[str, Any]


class PiDriver(Protocol):
    session_file: Path | None
    returncode: int | None

    def start(self) -> None: ...

    def prompt(self, text: str, *, timeout: float | None = None) -> TurnResult: ...

    def compact(self, arguments: Mapping[str, Any], *, timeout: float | None = None) -> Mapping[str, Any]: ...

    def restart(self, *, timeout: float | None = None) -> None: ...

    def checkpoint(self, *, timeout: float | None = None) -> DriverCheckpoint: ...

    def fork(self, entry_id: str, *, timeout: float | None = None) -> Mapping[str, Any]: ...

    def stop(self) -> None: ...


def make_driver(
    run: RunSpec,
    workspace: Workspace,
    event_sink: EventSink,
    stderr_sink: StderrSink,
) -> PiDriver:
    if run.scenario.driver == "json":
        from .json_driver import JsonDriver

        return JsonDriver(run, workspace, event_sink, stderr_sink)
    if run.scenario.driver == "rpc":
        from .rpc_driver import RpcDriver

        return RpcDriver(run, workspace, event_sink, stderr_sink)
    raise DriverError(f"unknown Pi driver: {run.scenario.driver}")
