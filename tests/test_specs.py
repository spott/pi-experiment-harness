from __future__ import annotations

from pathlib import Path

import pytest

from pi_experiments.matrix import expand_runs, expand_variants
from pi_experiments.scenario import load_experiment, load_scenario
from pi_experiments.spec import SpecError


def write_scenario(root: Path, *, driver: str = "rpc", steps: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "repo").mkdir()
    (root / "prompts").mkdir()
    (root / "prompts" / "first.md").write_text("Inspect the parser.\n", encoding="utf-8")
    path = root / "scenario.yaml"
    path.write_text(
        f"""name: long-session
repo:
  source: repo
  revision: main
driver: {driver}
steps:
{steps or '''  - prompt:
      file: prompts/first.md
  - checkpoint: after-inspection
  - evaluate:
      name: tests
      commands: pytest -q
      hidden: true
  - budget:
      max_cost_usd: 3
'''}""",
        encoding="utf-8",
    )
    return path


def write_experiment(root: Path) -> Path:
    path = root / "experiment.yaml"
    path.write_text(
        """name: compaction core
scenario: scenario.yaml
repetitions: 2
matrix:
  model:
    - id: provider/model-a
      thinking: high
    - id: provider/model-b
      thinking: medium
  configuration:
    - name: vanilla
      extensions: []
    - name: task-compaction
      extensions:
        - source: ../extension
run:
  timeout_seconds: 120
  results_dir: output
  credential_source: none
""",
        encoding="utf-8",
    )
    return path


def test_loads_and_resolves_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(write_scenario(tmp_path))

    assert scenario.name == "long-session"
    assert scenario.repo.source == (tmp_path / "repo").resolve()
    assert scenario.steps[0].operation == "prompt"
    assert scenario.steps[0].arguments["text"] == "Inspect the parser.\n"
    assert scenario.steps[0].arguments["source"]["content_hash"].startswith("sha256:")
    assert scenario.steps[1].arguments == {"name": "after-inspection"}
    assert scenario.steps[2].arguments["inject_result"] is False
    assert scenario.steps[2].arguments["commands"] == ["pytest -q"]


def test_expands_cartesian_matrix_and_unique_run_identity(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))

    variants = expand_variants(experiment.matrix)
    first = expand_runs(experiment, invocation_id="invocation-a")
    second = expand_runs(experiment, invocation_id="invocation-b")

    assert len(variants) == 4
    assert len(first) == 8
    assert len({run.run_id for run in first}) == 8
    assert {run.spec_fingerprint for run in first} == {
        run.spec_fingerprint for run in second
    }
    assert {run.run_id for run in first}.isdisjoint(run.run_id for run in second)
    assert all(run.options.results_dir == (tmp_path / "output").resolve() for run in first)
    assert all(run.scenario.steps[0].arguments["text"] == "Inspect the parser.\n" for run in first)


def test_variant_filter_rejects_unknown_id(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    with pytest.raises(ValueError, match="unknown variant"):
        expand_runs(experiment, invocation_id="x", variant_ids=["missing"])


def test_checked_in_examples_resolve() -> None:
    examples = Path(__file__).parents[1] / "examples"

    basic = load_experiment(examples / "basic" / "experiment.yaml")
    one_shot = load_experiment(examples / "one-shot" / "experiment.toml")
    basic_runs = expand_runs(basic, invocation_id="example-check")
    one_shot_runs = expand_runs(one_shot, invocation_id="example-check")

    assert len(basic_runs) == 8
    assert all(run.scenario.driver == "rpc" for run in basic_runs)
    assert len(one_shot_runs) == 1
    assert one_shot_runs[0].scenario.driver == "json"


def test_toml_scenario_is_supported(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    scenario_path = tmp_path / "scenario.toml"
    scenario_path.write_text(
        """name = "smoke"
driver = "json"

[repo]
source = "repo"
revision = "HEAD"

[[steps]]
prompt = { text = "Make a small change" }
""",
        encoding="utf-8",
    )
    scenario = load_scenario(scenario_path)
    assert scenario.driver == "json"
    assert scenario.steps[0].arguments == {"text": "Make a small change"}


@pytest.mark.parametrize(
    "steps, message",
    [
        (
            "  - prompt:\n      text: hello\n      file: prompt.md\n",
            "exactly one",
        ),
        (
            "  - made_up: {}\n",
            "unsupported operation",
        ),
        (
            "  - evaluate:\n      name: test\n      commands: []\n",
            "non-empty list",
        ),
    ],
)
def test_scenario_validation(tmp_path: Path, steps: str, message: str) -> None:
    write_scenario(tmp_path, steps=steps)
    with pytest.raises(SpecError, match=message):
        load_scenario(tmp_path / "scenario.yaml")


def test_json_driver_rejects_multiple_prompts(tmp_path: Path) -> None:
    write_scenario(
        tmp_path,
        driver="json",
        steps="  - prompt: {text: one}\n  - prompt: {text: two}\n",
    )
    with pytest.raises(SpecError, match="exactly one prompt"):
        load_scenario(tmp_path / "scenario.yaml")


def test_host_auth_requires_explicit_state_path(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    experiment = write_experiment(tmp_path)
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            "credential_source: none", "credential_source: host_auth"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="host_pi_state_dir"):
        load_experiment(experiment)
