# PoV Generator Foundation (M0-M4)

This repository now contains the first executable foundation of the system described in `specs/00..08`.

Implemented scope:

- `M0`: executable schemas-by-code and sample declarative corpus
- `M1`: registry for templates, recipes, and controlled vocabularies
- `M2`: versioned `ProblemState` store with patches, snapshots, and replay
- `M3`: task store, FSM, task events, and recipe progress projection
- `M4`: deterministic planning coordinator with dry-run and materialization

The current implementation is intentionally local and inspectable:

- registry source of truth: YAML files in [`templates`](F:\0work\python\PoV-generator\templates)
- runtime state: SQLite database inside a project workspace
- operator surface: CLI only
- execution layer, LLM, artifacts, and UI are not implemented yet

## Installation

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .[dev]
```

## Quick Start

Validate the declarative corpus:

```powershell
.\.venv\Scripts\povgen registry validate
```

Create a local project workspace:

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\demo_case `
  --name "Demo Requirements" `
  --recipe common.build_requirements_spec@1.0.0 `
  --request-text "Нужен сервис для формирования ТЗ по бизнес-запросу."
```

Inspect the initial state:

```powershell
.\.venv\Scripts\povgen problem show --workspace runtime\demo_case
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
```

Materialize the first planned step:

```powershell
.\.venv\Scripts\povgen plan apply --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks list --workspace runtime\demo_case
```

Simulate progress manually:

```powershell
.\.venv\Scripts\povgen tasks transition --workspace runtime\demo_case --task-id <task-id> --command start
.\.venv\Scripts\povgen problem readiness-set --workspace runtime\demo_case --dimension goal_clarity --status ready --blocking false
.\.venv\Scripts\povgen problem gap-close --workspace runtime\demo_case --gap-id unclear_goal
.\.venv\Scripts\povgen tasks transition --workspace runtime\demo_case --task-id <task-id> --command complete
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
```

## What You Can Verify Manually

1. Registry validation explains whether templates, recipes, and vocabularies are consistent.
2. `plan dry-run` shows:
   - which recipe step is next
   - which candidate templates were checked
   - why a step is admissible or blocked
   - why core work is not allowed to start too early
3. `problem show` and `problem history` show the current `ProblemState` and every applied patch.
4. `tasks list` and `tasks events` show how the runtime graph evolves.
5. `tasks recipe-progress` shows which recipe obligations are still pending.

## Project Layout

- [`templates`](F:\0work\python\PoV-generator\templates): declarative corpus for M0-M1
- [`src/pov_generator`](F:\0work\python\PoV-generator\src\pov_generator): implementation
- [`tests`](F:\0work\python\PoV-generator\tests): automated tests

## Deliberate Limits

This is a foundation slice, not the whole product.

Not implemented yet:

- artifact store and context engine (`M5`)
- LLM/runtime execution (`M6`)
- end-to-end generation from business request to real specification (`M7`)
- validation/governance hardening (`M8`)
- UI/operator workspace (`M9`)
