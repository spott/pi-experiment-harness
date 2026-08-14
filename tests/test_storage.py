from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_experiments.matrix import expand_runs
from pi_experiments.scenario import load_experiment
from pi_experiments.storage import ResultIndex, ResultStore

from .test_specs import write_experiment, write_scenario


def make_run(tmp_path: Path):
    write_scenario(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    run = expand_runs(experiment, invocation_id="storage-test", repetitions=1)[0]
    return experiment, run


def test_result_store_creates_immutable_bundle(tmp_path: Path) -> None:
    experiment, run = make_run(tmp_path / "spec")
    store = ResultStore(tmp_path / "results")
    experiment_dir = store.initialize_experiment(run.experiment_id, experiment, run.scenario)
    paths = store.create_run(run)

    assert (experiment_dir / "experiment.yaml").is_file()
    assert paths.run_spec.is_file()
    resolved = json.loads(paths.run_spec.read_text(encoding="utf-8"))
    assert resolved["run_id"] == run.run_id
    assert resolved["scenario"]["steps"][0]["arguments"]["text"] == "Inspect the parser.\n"

    store.append_jsonl(run, "events.jsonl", [{"type": "start"}, {"type": "end"}])
    assert len(paths.events.read_text(encoding="utf-8").splitlines()) == 2
    store.write_bytes(run, "stderr.log", b"diagnostic\n")
    store.finalize(run, {"status": "completed"})
    assert store.is_finalized(run.experiment_id, run.run_id)
    with pytest.raises(RuntimeError, match="immutable"):
        store.write_json(run, "metrics.json", {})
    with pytest.raises(FileExistsError, match="already exists"):
        store.create_run(run)


def test_result_store_rejects_path_escape(tmp_path: Path) -> None:
    experiment, run = make_run(tmp_path / "spec")
    store = ResultStore(tmp_path / "results")
    store.initialize_experiment(run.experiment_id, experiment, run.scenario)
    store.create_run(run)
    with pytest.raises(ValueError, match="inside its run"):
        store.write_bytes(run, "../outside", b"bad")


def test_sqlite_indexes_all_core_entities(tmp_path: Path) -> None:
    experiment, run = make_run(tmp_path / "spec")
    result_path = tmp_path / "results" / run.experiment_id / "runs" / run.run_id
    index = ResultIndex(tmp_path / "results" / "index.sqlite3")

    index.register_experiment(run.experiment_id, experiment, run.scenario)
    index.register_variant(run.experiment_id, run.variant)
    index.register_run(run, result_path)
    index.update_run(run.run_id, status="running", started_at="2026-01-01T00:00:00Z")
    index.add_turn(run.run_id, 1, {"usage": {"input": 10}})
    index.add_checkpoint(run.run_id, "after", 2, {"tree": "abc"})
    index.add_metrics(run.run_id, "generic", {"input_tokens": 10, "cost": None})
    index.add_evaluation(run.run_id, "tests", 3, 0, {"passed": True})
    index.add_artifact(run.run_id, "events.jsonl", "events", "sha256:abc", 42)
    index.update_run(
        run.run_id,
        status="completed",
        exit_reason="scenario_completed",
        process_exit_status=0,
        ended_at="2026-01-01T00:01:00Z",
    )

    row = index.get_run(run.run_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["exit_reason"] == "scenario_completed"
    assert index.list_runs(run.experiment_id)[0]["id"] == run.run_id

    with index.connect() as connection:
        assert connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM metrics").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
