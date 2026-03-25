# Miniflow

- [Getting Started](#getting-started)
  - [Structure](#structure)
  - [Setup](#setup)
  - [Commands](#commands)

- [Core Concepts](#core-concepts)
  - [Execution model](#execution-model)
  - [Database model (db)](#database-model)
  - [Integrations](#integrations)
  - [Jobs](#jobs)
  - [Flows](#flows)
  - [Inspect](#inspect)

- [Runtime](#runtime)
  - [Scheduling](#scheduling)
  - [Output verbosity](#output-verbosity)
  - [Execution logs](#execution-logs)
  - [Environment variables](#environment-variables)
  - [Environment constraints](#environment-constraints)

- [Governance](#governance)
  - [What Miniflow enforces](#what-miniflow-enforces)
  - [What not to do](#what-not-to-do)
  - [Principles](#principles)

---

Miniflow is a simple automation system for infrastructure operations, where workflows are explicitly defined and executed without hidden behavior.

It gives you full control over what runs, when it runs, and how it runs — without introducing orchestration complexity, implicit dependencies, or opaque systems.

Instead of trying to automate decisions, Miniflow focuses on making execution predictable, debuggable, and easy to reason about.

It separates four responsibilities:

| Layer | What it is | Who owns it |
|---|---|---|
| **Execution** | `run.py` — executes commands and enforces local constraints | Engine — do not modify |
| **Decision** | `flows/` — defines what to run and in what order | User |
| **Implementation** | `db/`, `jobs/`, `integrations/` — the actual SQL, scripts, and jobs | User |
| **Inspection** | `inspect/` — read-only diagnostics and environment inspection | User |

Miniflow does **not**:
- infer execution order
- resolve dependencies between targets
- inspect or interpret user code
- run anything automatically

Every command must be invoked deliberately — either via the CLI or inside a flow script.

All decisions must be defined before execution, typically in flows.

---

## Getting Started

#### Structure

```
run.py              # execution engine (do not modify)
miniflow_config.py  # user-editable configuration (CRITICAL, ENV_CONSTRAINTS)
crontab             # job schedule with supercronic (standard cron format)

db/                 # database state (layered SQL execution)
  bootstrap/        # one-time setup (schemas, roles, extensions)
  core/             # source-of-truth definitions
  derived/          # recomputable outputs
  functions/        # reusable functions
  seeds/            # static data
  rls/              # access policies
  dangerous/        # destructive operations (requires --confirm)

integrations/       # external system configuration (namespace-based)

jobs/               # self-contained execution units (namespace-based)
  <namespace>/
    <job>/
      apply.sh

flows/              # execution order (shell scripts calling run.py)

inspect/            # read-only diagnostics (not part of flows or pipelines)
```

#### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with real credentials
```

Environment variables are loaded from `.env` automatically if the file exists.
They override variables already set in the shell.

The setup process also ensures the scheduler binary (supercronic) is available locally.

#### Commands

```bash
# List all available targets
python run.py list

# Apply a DB file — flat or schema-qualified
python run.py db apply <layer> <target>
python run.py db apply <layer> <schema>.<target>   # resolves to db/<layer>/<schema>/<target>.sql

# CRITICAL targets require explicit confirmation
python run.py db apply <layer> <target> --critical
python run.py integrations apply <namespace>.<target> --critical
python run.py jobs apply <namespace>.<job> --critical

# Apply an integration script — namespace.target
python run.py integrations apply <namespace>.<target>

# Run a data pipeline job — namespace.job
python run.py jobs apply <namespace>.<job>

# Destructive — permanently deletes all data
python run.py db dangerous <target> --confirm

# Validate all run.py commands inside flows/
python run.py flows check

# Find targets not referenced in any flow
python run.py flows unused

# Find targets not referenced by a specific flow
python run.py flows unused <flow>

# Run a read-only inspect target — namespace.target
python run.py inspect <namespace>.<target>
```

## Core Concepts

#### Execution model

The CLI executes **one target at a time**. It does not know about other targets, does not track state, and does not enforce order. The system does not maintain shared execution state between targets.

Flows are the source of truth for execution order. A flow is a shell script in `flows/` that calls `run.py` commands in the correct sequence.

| Use case | Recommended approach |
|---|---|
| Debug a single target | CLI |
| Isolated re-run | CLI |
| Fresh environment setup | Flow |
| Multi-step production operation | Flow |
| Scheduled or automated execution | Flow |

Miniflow does not enforce execution order at runtime. Order is only defined by how commands are sequenced in flows.

There is no `apply-all`. Each target must be run explicitly. A typical fresh setup:

```
1. python run.py db apply bootstrap <target>      # repeat for each bootstrap target
2. python run.py db apply core <target>           # repeat for each core target
3. python run.py db apply derived <target>        # repeat for each derived target
4. python run.py db apply functions <target>      # repeat for each function target
5. python run.py db apply seed <target>           # repeat for each seed target
6. python run.py db apply rls <target>            # repeat for each rls target
7. python run.py jobs apply <namespace>.<job>     # repeat for each job
8. python run.py integrations apply <namespace>.<target> # repeat for each integration target
```

Flows are the source of truth for execution order. The sequence above belongs in a flow script under `flows/`.

<h4 id="database-model">Database model (db)</h4>

The database is **state-based, not migration-based**.

Targets are logical identifiers resolved to SQL files in `db/<layer>/`. They are applied with:

`python run.py db apply <layer> <target>`

The folder structure defines logical grouping and typical execution order, but Miniflow does not enforce dependencies.

| Layer | Purpose | Execution |
|---|---|---|
| `bootstrap/` | Extensions, schemas, roles | First |
| `core/` | Source-of-truth tables | After bootstrap |
| `derived/` | Recomputable tables | After data load |
| `functions/` | Reusable functions | Anytime |
| `seed/` | Static data | Anytime |
| `rls/` | Access policies | Last |

All targets should be **idempotent**. The system does not track execution state.

Targets listed in `CRITICAL` (defined in `miniflow_config.py`) require `--critical` to execute. Without it, the command fails with a clear error. This applies to `db apply`, `integrations apply`, and `jobs apply`.

#### Jobs

Jobs are self-contained execution units organized by namespace. Miniflow treats each job as a black box.

```
jobs/
  <namespace>/
    <job>/
      apply.sh
```

Example:

```bash
python run.py jobs apply mynamescapce.myjob
```

- Jobs expose a single entry point (`apply.sh`)
- Internal implementation (scripts, modules, dependencies) is not visible to Miniflow
- No other job or flow should depend on the internal steps of another job
- Only the outcome matters — not how the job achieves it

#### Integrations

Integrations are scripts used to configure external systems via APIs.

- Located in `integrations/<namespace>/<target>.sh`
- Executed with `python run.py integrations apply <namespace>.<target>`
- Treated as black boxes — the engine does not inspect their behavior
- Should be idempotent and define system configuration, not data processing

Use integrations for external system setup, not for database changes (`db/`) or data pipelines (`jobs/`).

#### Flows

Flows define the execution order of operations.

A flow is a shell script in `flows/` that calls `run.py` commands in sequence. They are the source of truth for how infrastructure is applied.

Miniflow does not infer or enforce order — flows make all decisions explicit.

##### Flow validation

`flows check` validates all executable `python run.py` commands inside the `flows/` directory:

```bash
python run.py flows check
```

It ensures that commands are complete, correctly structured, and ready to run.

Errors are reported when commands are incomplete, invalid, or contain unresolved placeholders (e.g. `<target>`). All errors are collected and displayed together.

```
Found 2 error(s) in flows:
  - flows/orchestrator-dev.sh:24 → unresolved placeholder in active command: python run.py db dangerous <target> --confirm
  - flows/orchestrator-dev.sh:25 → invalid integrations command: expected 'integrations apply <namespace.target>': python run.py integrations apply <namespace>
```

Warnings surface potential violations without blocking execution:

- Misuse of CRITICAL targets (missing or unnecessary `--critical`)
- Environment constraints that may block execution
- Layer order regressions that violate expected execution sequence

Exit code is `0` if all flows are valid, `1` otherwise. Warnings do not affect the exit code.

##### Unused targets

`flows unused` reports targets that exist in the repository but are not referenced in any flow:

```bash
python run.py flows unused
```

To scope the analysis to a single flow:

```bash
python run.py flows unused <flow>

# Examples:
python run.py flows unused orchestrator.sh
python run.py flows unused flows/orchestrator.sh
```

Output is grouped by domain:

```
[db]
  core.users
  derived.old_table

[jobs]
  ai.legacy_job

[integrations]
  geoserver.old_config
```

Unused targets are not errors — the command is informational only. Use it to find dead weight before removing files.

#### Inspect

`inspect` is a read-only execution domain for diagnostics. It is not part of pipelines or flows.

```bash
python run.py inspect <namespace.target>
```

Files live under `inspect/<namespace>/` and can be `.sql` or `.sh`:

```
inspect/
  db/
    <target>.sql        # executed via psql
  api/
    <target>.sh         # executed via shell
```

Resolution order for `<target>`:
1. `inspect/<namespace>/<target>.sql` — runs via `psql -f`
2. `inspect/<namespace>/<target>.sh` — runs via `bash`

If both exist → error. If neither exists → error. `psql` must be installed for SQL files.

Output goes directly to stdout. Nothing is logged to `.logs/infra.log`.

## Runtime

#### Scheduling

Miniflow does not implement scheduling logic, but this repository provides a scheduler binary for execution. This repository uses supercronic as the default scheduler.

supercronic is installed during setup and available as a local binary. It requires a Linux environment.

Flows should be used as the entry point for scheduled execution.

Example crontab:

```
*/5 * * * * bash flows/<flow>.sh
```

Execution:

```
./supercronic crontab
```

Miniflow does not handle job scheduling itself; it simply runs within the supercronic scheduler process. Advanced scheduling features such as timing, retries, or orchestration are not managed by Miniflow, so it's the user's responsibility to configure, run, and maintain the scheduler process.

#### Output verbosity

Output level is controlled automatically based on execution context. There is no CLI flag.

| Context | Level | Behavior |
|---|---|---|
| Interactive terminal (no `VERBOSE` set) | `2` | Full output: subprocess stdout/stderr and DB notices |
| Interactive terminal (`VERBOSE=1`) | `1` | Minimal output |
| Interactive terminal (`VERBOSE=2`) | `2` | Full output |
| Non-interactive (PM2, cron, API) | `1` | Always minimal — env var ignored |

To suppress output in an interactive session:

```bash
VERBOSE=1 python run.py db apply core <schema>.<target>
```

#### Execution logs

Every command execution is appended to `.logs/infra.log` as a single JSON line (JSONL):

```json
{"command": "db apply core.<schema>.<target>", "status": "success", "started_at": "2026-03-21T22:40:31.019802+00:00", "finished_at": "2026-03-21T22:40:31.120802+00:00", "duration_ms": 101}
```

| Field | Description |
|---|---|
| `command` | The command that was executed |
| `status` | `success` or `error` |
| `started_at` | UTC timestamp (ISO 8601) |
| `finished_at` | UTC timestamp (ISO 8601) |
| `duration_ms` | Execution duration in milliseconds |

Logs are always written — regardless of `VERBOSE` level — and are never truncated or rotated. Logging failures are silently ignored so they never interrupt execution.

#### Environment variables

User-defined environment variables (e.g. for jobs or integrations) are not defined by Miniflow and depend on each project.

| Variable | Required by | Default |
|---|---|---|
| `PGHOST` | all db commands | — |
| `PGUSER` | all db commands | — |
| `PGPASSWORD` | all db commands | — |
| `PGPORT` | all db commands | `5432` |
| `PGDATABASE` | all db commands | `postgres` |
| `VERBOSE` | all commands | `2` (interactive) / `1` (non-interactive) |
| `MINIFLOW_ENV` | all commands | — (required only for targets listed in `ENV_CONSTRAINTS`) |

#### Environment constraints

Targets can be restricted to specific environments using `ENV_CONSTRAINTS` in `miniflow_config.py`:

```python
ENV_CONSTRAINTS = {
    "integrations.<namespace>.<target>": ["prod"],
    "core.<schema>.<target>": ["dev"],
}
```

The active environment is read from `MINIFLOW_ENV`. Targets with no entry are unrestricted and always allowed.

| Condition | Behavior |
|---|---|
| Target has no constraint | Always allowed — `MINIFLOW_ENV` is ignored |
| Target has constraint, `MINIFLOW_ENV` matches | Allowed |
| Target has constraint, `MINIFLOW_ENV` does not match | Blocked |
| Target has constraint, `MINIFLOW_ENV` is not set | Blocked |

Error when environment is unset:

```
Error: target 'integrations.<namespace>.<target>' requires environment ['prod'], but MINIFLOW_ENV is not set.
```

Error when environment does not match:

```
Error: target 'integrations.<namespace>.<target>' is not allowed in environment 'dev'.
Allowed environments: ['prod']
```

The check runs before any SQL or script is executed. `

## Governance

#### What Miniflow enforces

Miniflow is strict about structure, but agnostic to content.

Miniflow enforces:

- **Directory structure** — targets must exist in the expected location (`db/<layer>/`, `jobs/<namespace>/<job>/`, `integrations/<namespace>/`, `inspect/<namespace>/`)
- **Execution conventions** — CRITICAL targets require `--critical`, dangerous targets require `--confirm`
- **Environment policy** — targets with environment constraints are blocked unless `MINIFLOW_ENV` matches `ENV_CONSTRAINTS`
- **Execution Ordering** — Ordering is defined in flows, not inferred by the engine

- **Agnosticism** — Miniflow does not interpret or depend on the internal implementation of targets

Anything not listed above is outside the system's responsibility.

#### What not to do

- **Do not modify `run.py`** — it is the execution engine. Configuration belongs in `miniflow_config.py`
- **Do not rely on implicit execution order** — Miniflow does not infer or enforce order at runtime
- **Do not use the CLI for multi-step operations** — use a flow script in `flows/`
- **Do not create dependencies between jobs** — each job must be independently executable
- **Flows should not contain business logic** — only orchestration via `run.py` commands

#### Principles

- Explicit over implicit: nothing is auto-discovered or run automatically
- Safety by default: destructive operations require `--confirm`
- Idempotent: all commands are safe to run multiple times
- Automation: workflows are explicitly defined

---
