"""External command evaluator."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CommandEvaluation:
    name: str
    commands: tuple[Mapping[str, Any], ...]
    passed: bool
    elapsed_seconds: float


def evaluate_commands(
    name: str,
    commands: Sequence[str],
    *,
    repository: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> CommandEvaluation:
    started = time.monotonic()
    deadline = started + timeout_seconds
    results: list[Mapping[str, Any]] = []
    passed = True
    for command in commands:
        command_started = time.monotonic()
        remaining = deadline - command_started
        if remaining <= 0:
            results.append(
                {
                    "command": command,
                    "exit_status": None,
                    "timed_out": True,
                    "skipped_reason": "evaluation deadline exhausted",
                    "stdout": "",
                    "stderr": "",
                    "elapsed_seconds": 0.0,
                }
            )
            passed = False
            continue
        try:
            process = subprocess.run(
                ["/bin/sh", "-lc", command],
                cwd=repository,
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
            )
            result: dict[str, Any] = {
                "command": command,
                "exit_status": process.returncode,
                "timed_out": False,
                "stdout": process.stdout.decode("utf-8", errors="replace"),
                "stderr": process.stderr.decode("utf-8", errors="replace"),
                "elapsed_seconds": time.monotonic() - command_started,
            }
            passed = passed and process.returncode == 0
        except subprocess.TimeoutExpired as exc:
            result = {
                "command": command,
                "exit_status": None,
                "timed_out": True,
                "stdout": (exc.stdout or b"").decode("utf-8", errors="replace"),
                "stderr": (exc.stderr or b"").decode("utf-8", errors="replace"),
                "elapsed_seconds": time.monotonic() - command_started,
            }
            passed = False
        results.append(result)
    return CommandEvaluation(
        name=name,
        commands=tuple(results),
        passed=passed,
        elapsed_seconds=time.monotonic() - started,
    )
