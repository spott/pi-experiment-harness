# Pi Experiment Harness

A reproducible scenario harness for comparing [Pi coding agent](https://github.com/badlogic/pi-mono) models, extensions, resources, and configurations. Each run executes at a fixed Git revision in a detached disposable worktree. Raw filesystem artifacts are the source of truth; SQLite is a query index.

## What works

- YAML and TOML experiment/scenario files
- Cartesian model/configuration matrices and repetitions
- One-shot Pi JSON and long-lived Pi RPC drivers
- Prompt, checkpoint, evaluator, budget, manual compaction, restart, and fork steps
- Isolated Pi state/session directories and explicit credential selection
- Raw Pi events/session/stderr, evaluator logs, Git patches, provenance, and checkpoints
- Generic usage/cost/tool/Git/evaluator metrics
- Metrics for version-1 Pi task-compaction markers
- `run`, `show`, `compare`, and dry-run-first `gc` commands

The implemented isolation backend is **fast isolation**, not a security boundary. See [Limitations](#limitations).

## Requirements and installation

- Python 3.11+
- Git
- Pi 0.84.x available as `pi` (or set `run.pi_executable`)
- Provider credentials only if the selected model needs them

For development:

```sh
uv sync --group dev
uv run piexp --help
uv run pytest -q
```

To build/install:

```sh
uv build
uv tool install dist/pi_experiment_harness-0.1.0-py3-none-any.whl
piexp --help
```

## Quick start

1. Copy `examples/basic/` and change `scenario.yaml`'s `repo.source` to the Git repository under test.
2. Change the illustrative model IDs (availability depends on your Pi/provider setup) and remove or update the local extension path in `experiment.yaml`.
3. Make the required provider credentials available according to `credential_source`.
4. Run and inspect the experiment:

```sh
uv run piexp run examples/basic/experiment.yaml
uv run piexp show --results-dir results RUN_ID_OR_UNIQUE_PREFIX
uv run piexp compare --results-dir results EXPERIMENT_ID_OR_UNIQUE_PREFIX
```

`piexp run` prints the resolved experiment ID, every run ID/status, and artifact paths as JSON. Use a new `--invocation-id` for a new invocation; reusing one deliberately generates the same run IDs and will collide with immutable existing bundles.

Useful run selection options:

```sh
piexp run experiment.yaml --variant vanilla --repeat 3
piexp run experiment.yaml --results-dir /data/piexp-results
```

A variant selector can be an exact variant ID, full generated variant name, or configuration name. A configuration name can select multiple model variants.

## Specification reference

Paths are resolved relative to the document containing them. Prompt files are read and hashed while resolving the run, before Pi starts.

### Scenario

```yaml
name: parser-task
repo:
  source: ../target-project
  revision: main
driver: rpc
steps:
  - budget:
      max_cost_usd: 2
      max_model_turns: 10
      max_wall_seconds: 900
  - prompt:
      file: prompts/task.md
  - checkpoint: inspected
  - evaluate:
      name: tests
      commands: ["pytest -q"]
      timeout_seconds: 300
      inject_result: false
  - compact: {}
  - restart_pi: {}
  - prompt:
      text: Re-check the failure and finish the task.
  - checkpoint: final
```

| Step | JSON | RPC | Notes |
|---|---:|---:|---|
| `prompt` | One exactly | Yes | Exactly one of `text` or `file` |
| `checkpoint` | Degraded | Yes | JSON captures Git only; RPC also captures state/stats/entries/tree |
| `evaluate` | Yes | Yes | Shell commands run in the disposable worktree; results stay outside the conversation unless `inject_result: true` |
| `budget` | Yes | Yes | Activates cost, model-turn, and/or wall limits for following work |
| `compact` | No | Yes | Requests Pi global compaction |
| `restart_pi` | No | Yes | Restarts and resumes the exact reported session file |
| `fork` | No | Yes | Requires a concrete Pi entry ID |
| `switch_branch` | No | No | Parsed but fails explicitly; Pi 0.84 RPC has no arbitrary-leaf switch command |

A scenario-level `run.timeout_seconds` always bounds driver/evaluator operations. Evaluator `timeout_seconds` bounds the whole command list, not each command separately. Evaluator failure is recorded as data and does not itself fail the harness run. `inject_result: true` creates another RPC user turn. `hidden: true` is rejected under fast isolation because hidden evaluator assets require a strict boundary.

### Experiment

```yaml
name: parser-comparison
scenario: scenario.yaml
repetitions: 2
matrix:
  model:
    - provider: anthropic
      id: MODEL_ID
      thinking: medium
  configuration:
    - name: vanilla
      extensions: []
    - name: candidate
      extensions:
        - source: ../extension
run:
  isolation: fast
  timeout_seconds: 1200
  results_dir: results
  pi_executable: pi
  credential_source: environment
```

Every matrix axis is expanded with every other axis. `model` and `configuration` have runtime meaning; additional axes are retained in variant identity/provenance. Supported model fields are `provider`, `id`, and `thinking`.

Supported configuration fields:

- `extensions`, `skills`, `prompt_templates`: package names or local paths; local resources are hashed
- `tools`, `exclude_tools`: Pi tool allow/deny lists
- `context_files`, `approve_project`: opt into context discovery/project approval
- `system_prompt`, `append_system_prompts`
- `settings`: written to the isolated Pi state as `settings.json`
- `environment`: scalar environment overrides

Pi resource discovery is deny-by-default. The harness starts Pi with extensions, skills, prompt templates, themes, and context files disabled, then adds only declared resources. Per-resource extension configuration is not executable in this release and fails explicitly instead of being ignored.

### Credentials

`run.credential_source` is mandatory in the experimental boundary even though its loader default is `environment`:

- `environment`: inherit provider variables from the harness process
- `host_auth`: require `host_pi_state_dir`, scrub provider variables, and copy only `auth.json` into the run's isolated state with mode `0600`
- `none`: scrub known provider credential variables

Do not put secrets in `configuration.environment`: resolved run specs and provenance are persisted. Prefer process environment variables or isolated host auth.

## Result layout

```text
results/
  index.sqlite3
  EXPERIMENT_ID/
    experiment.yaml
    scenario.yaml
    experiment-meta.json
    runs/RUN_ID/
      run-spec.json
      events.jsonl
      session.jsonl
      stderr.log
      turns/
      checkpoints/
      evaluator.json
      evaluator-stdout.log
      evaluator-stderr.log
      final-response.md
      baseline.txt
      git-status.txt
      patch.diff
      patch-stat.txt
      changed-files.json
      final-tree.txt
      metrics.json
      completed.json
```

`completed.json` makes a bundle immutable. Failed setup/execution and exceeded budgets are also finalized outcomes with `error.json` or `budget.json`. SQLite stores queryable run entities and derived metrics, but artifacts remain authoritative.

Generic metrics include actual persisted-session usage/cost, turn context trajectories, tool calls/failures, compactions, session growth, Git change counts, and evaluator outcomes. Task-compaction metrics include marker pairing, open/closed regions, raw/summary sizes, compression ratios, expansions, and explicit acceptance diagnostics when present. The harness reports unavailable measurements as `null`; for example, provider-request projection diagnostics are required to prove raw-body reintroduction after global compaction.

`compare` reports count/min/median/mean/max for each numeric top-level metric by variant. It does not calculate a composite score or statistical significance.

## Workspace garbage collection

Normal runs remove their detached worktrees. To inspect stale `piexp-*` temporary directories left by interrupted processes:

```sh
piexp gc --older-than-hours 24          # preview only
piexp gc --older-than-hours 24 --apply  # destructive
```

`gc` only considers directories named `piexp-*` under the selected temporary root and is a dry run unless `--apply` is provided.

## Limitations

- `isolation: fast` isolates the worktree and Pi state/session, but shares the host kernel, network, process visibility, environment baseline, and potentially provider caches. It is not suitable for hostile code or hidden-test secrecy.
- `isolation: strict` is accepted by the schema but intentionally raises `NotImplementedError`; container isolation is not implemented.
- Runs execute sequentially. `max_parallel_per_provider` is recorded but not yet used for scheduling.
- Pi 0.84 RPC cannot switch the active leaf to an arbitrary existing tree entry.
- Parquet export, plots, inferential statistics, provider-cache mutation experiments, and request-projection diagnostics are not implemented.
- No live provider call is part of the automated test suite; subprocess behavior is integration-tested with a protocol-compatible fake Pi.

## Verification

```sh
uv run pytest -q
uv run python -m compileall -q src tests
uv build
```

The tests use real temporary Git repositories/worktrees and fake Pi JSON/RPC subprocesses. They verify canonical repository non-mutation, restart/resume, checkpoints, evaluators, budgets, artifact finalization, metrics, CLI workflows, and cleanup without consuming provider credentials.
