"""Expansion of experiment matrices into immutable run specifications."""

from __future__ import annotations

import itertools
import uuid
from typing import Any, Iterable, Mapping

from .paths import safe_component, slugify
from .scenario import load_scenario
from .serde import object_hash, short_hash
from .spec import ExperimentSpec, RunSpec, VariantSpec


def _axis_label(axis: str, value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("name", "id"):
            label = value.get(key)
            if isinstance(label, str) and label:
                return label
    return str(value)


def expand_variants(matrix: Mapping[str, tuple[Any, ...]]) -> tuple[VariantSpec, ...]:
    axes = tuple(matrix)
    variants: list[VariantSpec] = []
    for values in itertools.product(*(matrix[axis] for axis in axes)):
        selection = dict(zip(axes, values, strict=True))
        fingerprint = object_hash(selection)
        labels = [slugify(_axis_label(axis, selection[axis])) for axis in axes]
        base = "--".join(labels)
        identifier = f"{base}-{fingerprint.split(':', 1)[1][:8]}"
        variants.append(
            VariantSpec(
                id=safe_component(identifier, label="variant id"),
                name=" / ".join(_axis_label(axis, selection[axis]) for axis in axes),
                axes=selection,
                fingerprint=fingerprint,
            )
        )
    return tuple(variants)


def expand_runs(
    experiment: ExperimentSpec,
    *,
    invocation_id: str | None = None,
    repetitions: int | None = None,
    variant_ids: Iterable[str] | None = None,
) -> tuple[RunSpec, ...]:
    scenario = load_scenario(experiment.scenario_path)
    invocation = safe_component(invocation_id or uuid.uuid4().hex, label="invocation id")
    repeat_count = experiment.repetitions if repetitions is None else repetitions
    if repeat_count < 1:
        raise ValueError("repetitions must be positive")
    selected_ids = set(variant_ids) if variant_ids is not None else None
    experiment_id = f"{slugify(experiment.name)}-{short_hash({'experiment': experiment.raw, 'scenario': scenario}, 10)}"
    runs: list[RunSpec] = []
    variants = expand_variants(experiment.matrix)
    if selected_ids is not None:
        known = {variant.id for variant in variants}
        unknown = selected_ids - known
        if unknown:
            raise ValueError(f"unknown variant id(s): {', '.join(sorted(unknown))}")
        variants = tuple(variant for variant in variants if variant.id in selected_ids)
    for variant in variants:
        for repetition in range(1, repeat_count + 1):
            logical = {
                "schema_version": 1,
                "experiment": experiment.name,
                "scenario": scenario,
                "variant": variant,
                "repetition": repetition,
                "options": experiment.run,
            }
            fingerprint = object_hash(logical)
            nonce = short_hash(
                {"fingerprint": fingerprint, "invocation": invocation, "repetition": repetition},
                10,
            )
            run_id = safe_component(
                f"{variant.id}-r{repetition:03d}-{nonce}", label="run id"
            )
            runs.append(
                RunSpec(
                    schema_version=1,
                    experiment_id=experiment_id,
                    run_id=run_id,
                    invocation_id=invocation,
                    repetition=repetition,
                    spec_fingerprint=fingerprint,
                    experiment_name=experiment.name,
                    variant=variant,
                    scenario=scenario,
                    options=experiment.run,
                )
            )
    return tuple(runs)
