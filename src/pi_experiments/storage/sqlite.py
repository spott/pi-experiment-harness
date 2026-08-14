"""Queryable SQLite index over filesystem result bundles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..serde import to_primitive
from ..spec import ExperimentSpec, RunSpec, ScenarioSpec, VariantSpec

_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scenarios (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    driver TEXT NOT NULL,
    spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    axes_json TEXT NOT NULL,
    PRIMARY KEY (experiment_id, id)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    variant_id TEXT NOT NULL,
    scenario_hash TEXT NOT NULL REFERENCES scenarios(content_hash),
    invocation_id TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    spec_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_reason TEXT,
    process_exit_status INTEGER,
    started_at TEXT,
    ended_at TEXT,
    result_path TEXT NOT NULL,
    FOREIGN KEY (experiment_id, variant_id) REFERENCES variants(experiment_id, id)
);
CREATE TABLE IF NOT EXISTS turns (
    run_id TEXT NOT NULL REFERENCES runs(id),
    turn_index INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, turn_index)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    scenario_step INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL REFERENCES runs(id),
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (run_id, namespace, key)
);
CREATE TABLE IF NOT EXISTS evaluations (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    scenario_step INTEGER NOT NULL,
    exit_status INTEGER,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, name, scenario_step)
);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL REFERENCES runs(id),
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_hash TEXT,
    size_bytes INTEGER,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS runs_experiment_status ON runs(experiment_id, status);
CREATE INDEX IF NOT EXISTS metrics_lookup ON metrics(namespace, key);
"""


def _json(value: Any) -> str:
    return json.dumps(to_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ResultIndex:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def register_experiment(
        self, experiment_id: str, experiment: ExperimentSpec, scenario: ScenarioSpec
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO experiments(id,name,source_path,content_hash) VALUES(?,?,?,?)",
                (experiment_id, experiment.name, str(experiment.source_path), experiment.content_hash),
            )
            existing = connection.execute(
                "SELECT content_hash FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if existing is None or existing["content_hash"] != experiment.content_hash:
                raise ValueError(f"experiment id collision: {experiment_id}")
            connection.execute(
                "INSERT OR IGNORE INTO scenarios(content_hash,name,source_path,driver,spec_json) VALUES(?,?,?,?,?)",
                (
                    scenario.content_hash,
                    scenario.name,
                    str(scenario.source_path),
                    scenario.driver,
                    _json(scenario),
                ),
            )

    def register_variant(self, experiment_id: str, variant: VariantSpec) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO variants(experiment_id,id,name,fingerprint,axes_json) VALUES(?,?,?,?,?) "
                "ON CONFLICT(experiment_id,id) DO UPDATE SET name=excluded.name, "
                "fingerprint=excluded.fingerprint, axes_json=excluded.axes_json",
                (
                    experiment_id,
                    variant.id,
                    variant.name,
                    variant.fingerprint,
                    _json(variant.axes),
                ),
            )

    def register_run(self, run: RunSpec, result_path: str | Path, status: str = "pending") -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs(id,experiment_id,variant_id,scenario_hash,invocation_id,repetition," 
                "spec_fingerprint,status,result_path) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.experiment_id,
                    run.variant.id,
                    run.scenario.content_hash,
                    run.invocation_id,
                    run.repetition,
                    run.spec_fingerprint,
                    status,
                    str(Path(result_path).resolve()),
                ),
            )

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        exit_reason: str | None = None,
        process_exit_status: int | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status=?,exit_reason=COALESCE(?,exit_reason),"
                "process_exit_status=COALESCE(?,process_exit_status),"
                "started_at=COALESCE(?,started_at),ended_at=COALESCE(?,ended_at) WHERE id=?",
                (status, exit_reason, process_exit_status, started_at, ended_at, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def add_turn(self, run_id: str, turn_index: int, data: Any) -> None:
        self._replace(
            "INSERT OR REPLACE INTO turns(run_id,turn_index,data_json) VALUES(?,?,?)",
            (run_id, turn_index, _json(data)),
        )

    def add_checkpoint(self, run_id: str, name: str, scenario_step: int, data: Any) -> None:
        self._replace(
            "INSERT OR REPLACE INTO checkpoints(run_id,name,scenario_step,data_json) VALUES(?,?,?,?)",
            (run_id, name, scenario_step, _json(data)),
        )

    def add_metrics(self, run_id: str, namespace: str, values: dict[str, Any]) -> None:
        rows = [(run_id, namespace, key, _json(value)) for key, value in values.items()]
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO metrics(run_id,namespace,key,value_json) VALUES(?,?,?,?)",
                rows,
            )

    def add_evaluation(
        self,
        run_id: str,
        name: str,
        scenario_step: int,
        exit_status: int | None,
        data: Any,
    ) -> None:
        self._replace(
            "INSERT OR REPLACE INTO evaluations(run_id,name,scenario_step,exit_status,data_json) VALUES(?,?,?,?,?)",
            (run_id, name, scenario_step, exit_status, _json(data)),
        )

    def add_artifact(
        self,
        run_id: str,
        path: str,
        kind: str,
        content_hash: str | None,
        size_bytes: int | None,
    ) -> None:
        self._replace(
            "INSERT OR REPLACE INTO artifacts(run_id,path,kind,content_hash,size_bytes) VALUES(?,?,?,?,?)",
            (run_id, path, kind, content_hash, size_bytes),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def find_run(self, run_id_or_prefix: str) -> dict[str, Any] | None:
        exact = self.get_run(run_id_or_prefix)
        if exact is not None:
            return exact
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE id LIKE ? ORDER BY id LIMIT 2",
                (run_id_or_prefix + "%",),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"run id prefix is ambiguous: {run_id_or_prefix}")
        return dict(rows[0]) if rows else None

    def list_experiments(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM experiments ORDER BY created_at,id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE experiment_id=? ORDER BY variant_id,repetition,id",
                (experiment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_variants(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,name,fingerprint,axes_json FROM variants "
                "WHERE experiment_id=? ORDER BY id",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "fingerprint": row["fingerprint"],
                "axes": json.loads(row["axes_json"]),
            }
            for row in rows
        ]

    def get_metrics(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT namespace,key,value_json FROM metrics WHERE run_id=? "
                "ORDER BY namespace,key",
                (run_id,),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result.setdefault(row["namespace"], {})[row["key"]] = json.loads(
                row["value_json"]
            )
        return result

    def get_evaluations(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name,scenario_step,exit_status,data_json FROM evaluations "
                "WHERE run_id=? ORDER BY scenario_step,name",
                (run_id,),
            ).fetchall()
        return [
            {
                "name": row["name"],
                "scenario_step": row["scenario_step"],
                "exit_status": row["exit_status"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT path,kind,content_hash,size_bytes FROM artifacts "
                "WHERE run_id=? ORDER BY path",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _replace(self, sql: str, parameters: Iterable[Any]) -> None:
        with self.connect() as connection:
            connection.execute(sql, tuple(parameters))
