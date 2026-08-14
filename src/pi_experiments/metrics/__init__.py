"""Derived metric extraction from immutable raw run artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import MetricExtractor, load_artifacts
from .generic import GenericExtractor
from .task_compaction import TaskCompactionExtractor

DEFAULT_EXTRACTORS: tuple[MetricExtractor, ...] = (
    GenericExtractor(),
    TaskCompactionExtractor(),
)


def extract_metrics(
    run_directory: str | Path,
    completion: Mapping[str, Any] | None = None,
    extractors: Sequence[MetricExtractor] = DEFAULT_EXTRACTORS,
) -> dict[str, Any]:
    artifacts = load_artifacts(run_directory)
    namespaces: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for extractor in extractors:
        try:
            namespaces[extractor.namespace] = dict(extractor.extract(artifacts, completion))
        except Exception as exc:  # Raw artifacts stay useful if a plugin cannot parse them.
            errors.append(
                {
                    "namespace": extractor.namespace,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return {"schema_version": 1, "namespaces": namespaces, "extractor_errors": errors}


__all__ = [
    "DEFAULT_EXTRACTORS",
    "GenericExtractor",
    "MetricExtractor",
    "TaskCompactionExtractor",
    "extract_metrics",
    "load_artifacts",
]
