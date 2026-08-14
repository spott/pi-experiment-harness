"""Path and identifier validation."""

from __future__ import annotations

import re
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def slugify(value: str, *, fallback: str = "item") -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or fallback


def safe_component(value: str, *, label: str = "identifier") -> str:
    if not _SAFE_COMPONENT_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def relative_to(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()
