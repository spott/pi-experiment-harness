"""Build isolated Pi process arguments and environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..spec import RunSpec
from ..workspace.base import Workspace
from .base import DriverError

_EXTENSION_FLAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_HARNESS_OWNED_FLAGS = {
    "mode",
    "session",
    "session-dir",
    "extension",
    "no-extensions",
    "skill",
    "no-skills",
    "prompt-template",
    "no-prompt-templates",
    "theme",
    "no-themes",
    "context-file",
    "no-context-files",
    "approve",
    "no-approve",
    "model",
    "provider",
    "thinking",
    "tools",
    "exclude-tools",
    "system-prompt",
    "append-system-prompt",
}

_CREDENTIAL_VARIABLES = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANT_LING_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_RESOURCE_NAME",
    "AZURE_OPENAI_DEPLOYMENT_NAME_MAP",
    "DEEPSEEK_API_KEY",
    "NVIDIA_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "BASETEN_API_KEY",
    "OPENROUTER_API_KEY",
    "AI_GATEWAY_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
    "MISTRAL_API_KEY",
    "MINIMAX_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENCODE_API_KEY",
    "KIMI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_GATEWAY_ID",
    "QWEN_TOKEN_PLAN_API_KEY",
    "QWEN_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_API_KEY",
    "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
}


def _configuration(run: RunSpec) -> Mapping[str, Any]:
    return run.variant.configuration


def process_environment(run: RunSpec, workspace: Workspace) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PI_CODING_AGENT_DIR"] = str(workspace.pi_state)
    environment["PI_CODING_AGENT_SESSION_DIR"] = str(workspace.sessions)
    environment.setdefault("PI_TELEMETRY", "0")
    if run.options.credential_source != "environment":
        for name in _CREDENTIAL_VARIABLES:
            environment.pop(name, None)
    if run.options.credential_source == "host_auth":
        assert run.options.host_pi_state_dir is not None
        source = run.options.host_pi_state_dir / "auth.json"
        if not source.is_file():
            raise DriverError(f"host auth file does not exist: {source}")
        destination = workspace.pi_state / "auth.json"
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    configuration = _configuration(run)
    extra_environment = configuration.get("environment", {})
    if not isinstance(extra_environment, Mapping):
        raise DriverError("configuration.environment must be a mapping")
    for name, value in extra_environment.items():
        if not isinstance(name, str) or not isinstance(value, (str, int, float, bool)):
            raise DriverError("configuration.environment must contain scalar string-keyed values")
        if run.options.credential_source != "environment" and name in _CREDENTIAL_VARIABLES:
            raise DriverError(
                f"configuration.environment.{name} conflicts with credential_source "
                f"{run.options.credential_source!r}"
            )
        environment[name] = str(value)
    return environment


def process_arguments(run: RunSpec, workspace: Workspace, *, mode: str) -> list[str]:
    configuration = _configuration(run)
    executable = shutil.which(run.options.pi_executable) or run.options.pi_executable
    arguments = [
        executable,
        "--mode",
        mode,
        "--session-dir",
        str(workspace.sessions),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    ]
    model = run.variant.model
    model_id = model.get("id")
    if model_id:
        arguments.extend(["--model", str(model_id)])
    provider = model.get("provider")
    if provider:
        arguments.extend(["--provider", str(provider)])
    thinking = model.get("thinking")
    if thinking:
        arguments.extend(["--thinking", str(thinking)])
    if configuration.get("context_files") is True:
        arguments.remove("--no-context-files")
    if configuration.get("approve_project") is True:
        arguments[arguments.index("--no-approve")] = "--approve"
    tools = configuration.get("tools")
    if tools is not None:
        if not isinstance(tools, Sequence) or isinstance(tools, str) or not all(
            isinstance(item, str) for item in tools
        ):
            raise DriverError("configuration.tools must be a list of tool names")
        arguments.extend(["--tools", ",".join(tools)])
    excluded = configuration.get("exclude_tools")
    if excluded is not None:
        if not isinstance(excluded, Sequence) or isinstance(excluded, str) or not all(
            isinstance(item, str) for item in excluded
        ):
            raise DriverError("configuration.exclude_tools must be a list of tool names")
        arguments.extend(["--exclude-tools", ",".join(excluded)])
    for field, flag in (
        ("extensions", "--extension"),
        ("skills", "--skill"),
        ("prompt_templates", "--prompt-template"),
    ):
        resources = configuration.get(field, [])
        if not isinstance(resources, Sequence) or isinstance(resources, str):
            raise DriverError(f"configuration.{field} must be a list")
        for resource in resources:
            if isinstance(resource, str):
                source = resource
            elif isinstance(resource, Mapping) and isinstance(resource.get("source"), str):
                source = str(resource["source"])
                unsupported = set(resource) - {"source", "content_hash"}
                if unsupported:
                    raise DriverError(
                        f"configuration.{field} resource fields are not executable yet: "
                        + ", ".join(sorted(unsupported))
                    )
            else:
                raise DriverError(f"configuration.{field} entries require a source")
            arguments.extend([flag, source])
    extension_flags = configuration.get("extension_flags", {})
    if not isinstance(extension_flags, Mapping):
        raise DriverError("configuration.extension_flags must be a mapping")
    for name, value in extension_flags.items():
        if (
            not isinstance(name, str)
            or _EXTENSION_FLAG.fullmatch(name) is None
            or name in _HARNESS_OWNED_FLAGS
        ):
            raise DriverError(
                f"configuration.extension_flags contains unsafe or harness-owned flag {name!r}"
            )
        if not isinstance(value, (str, int, float, bool)):
            raise DriverError(
                f"configuration.extension_flags.{name} must be a scalar value"
            )
        encoded = str(value).lower() if isinstance(value, bool) else str(value)
        arguments.extend([f"--{name}", encoded])
    if "system_prompt" in configuration:
        arguments.extend(["--system-prompt", str(configuration["system_prompt"])])
    append_prompts = configuration.get("append_system_prompts", [])
    if not isinstance(append_prompts, Sequence) or isinstance(append_prompts, str):
        raise DriverError("configuration.append_system_prompts must be a list")
    for value in append_prompts:
        arguments.extend(["--append-system-prompt", str(value)])
    settings = configuration.get("settings", {})
    if not isinstance(settings, Mapping):
        raise DriverError("configuration.settings must be a mapping")
    if settings:
        (workspace.pi_state / "settings.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return arguments


def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        relative = item.name if path.is_file() else item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
        data = item.read_bytes()
        digest.update(data)
        size += len(data)
    return f"sha256:{digest.hexdigest()}", size


def resource_provenance(run: RunSpec) -> dict[str, Any]:
    result: dict[str, Any] = {}
    configuration = _configuration(run)
    for field in ("extensions", "skills", "prompt_templates"):
        records: list[dict[str, Any]] = []
        for resource in configuration.get(field, []):
            source = resource if isinstance(resource, str) else resource.get("source")
            record: dict[str, Any] = {"source": source}
            if isinstance(source, str):
                path = Path(source).expanduser()
                if path.exists():
                    digest, size = _hash_path(path.resolve())
                    record.update(
                        {"canonical_path": str(path.resolve()), "content_hash": digest, "size_bytes": size}
                    )
                    process = subprocess.run(
                        ["git", "-C", str(path if path.is_dir() else path.parent), "rev-parse", "HEAD"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    if process.returncode == 0:
                        record["repository_sha"] = process.stdout.strip()
                else:
                    record["unresolved_package_source"] = True
            if isinstance(resource, Mapping) and "config" in resource:
                record["config"] = resource["config"]
            records.append(record)
        result[field] = records
    return result


def pi_provenance(run: RunSpec, workspace: Workspace) -> dict[str, Any]:
    arguments = process_arguments(run, workspace, mode=run.scenario.driver)
    executable = Path(arguments[0])
    try:
        version = subprocess.run(
            [str(executable), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DriverError(f"cannot identify Pi executable {executable}: {exc}") from exc
    redacted_arguments = list(arguments)
    return {
        "pi": {
            "executable": str(executable.resolve()) if executable.exists() else str(executable),
            "version": version,
            "driver": run.scenario.driver,
            "arguments": redacted_arguments,
            "state_isolated": True,
            "session_isolated": True,
        },
        "model": dict(run.variant.model),
        "configuration": dict(run.variant.configuration),
        "credentials": {
            "source": run.options.credential_source,
            "environment_variables_present": sorted(
                name for name in _CREDENTIAL_VARIABLES if name in os.environ
            )
            if run.options.credential_source == "environment"
            else [],
        },
        "resources": resource_provenance(run),
    }
