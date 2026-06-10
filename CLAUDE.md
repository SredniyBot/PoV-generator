# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

Backend (run from repo root with `.venv` activated):

```bash
python -m pytest -q                                 # full test suite
python -m pytest tests/test_foundation.py -q        # one file
python -m pytest tests/test_foo.py::test_bar -q     # one test
python -m ruff check src tests                      # lint (same config as CI)
python -m pov_generator registry validate           # registry integrity check
povgen-api --reload                                 # dev API at :8788
povgen workflow run-until-blocked --workspace runtime/demo
```

UI (run from `ui/workspace/`):

```bash
npm ci
npm run build      # tsc --noEmit + vite build (CI's gate)
npm run dev        # vite dev server on :5173, proxies /api + /ws to :8788
```

CI matrix is Linux × Windows × macOS × Python 3.11/3.12 — keep code cross-platform (no shell-isms in tests, no path separator assumptions).

## Architecture — load-bearing concepts

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first; the source of truth for contracts is [`specs/`](specs/) (`00 → 12`). Highlights that take cross-file reading:

**Four-layer backend** (`src/pov_generator/`):
- `domain/` — pure dataclasses, no I/O (registry specs, ProjectKnowledge, ProcessState, ExecutionResult, etc.).
- `application/` — orchestration services. Each leaf-task run flows: methodology wrapper → `ContextManifest` → provider → 3 artifacts (primary + reasoning + methodology_trace) → validation → `ClarificationCoordinator` → patches → planner.
- `infrastructure/` — `sqlite_runtime.py` (event log + projections), `filesystem_registry.py` (YAML loader), `llm_settings_store.py` + `secret_box.py` (encrypted settings), and the `llm/` provider package (see "Provider switching").
- `interfaces/` — `api.py` (FastAPI + WebSocket) and `cli.py` (`povgen`).
- `common/` — cross-cutting helpers shared by all layers (`env.py`, `errors.py`, `serialization.py`); no business logic.

**Declarative registry** (`templates/`) — seven `kind`s, one YAML object per file: `objective`, `task_template`, `artifact_contract`, `domain_pack`, `methodology_pack`, `quality_gate`, `vocabulary`. Refs are always `<id>@<semver>` (no `latest`). Contracts never mutate retroactively — bump `version` instead. `test_foundation.py::test_registry_validation_passes_for_task_graph_corpus` gates registry integrity.

**Hard orthogonality** between `methodology_pack` ("how we think": stages + rules) and `domain_pack` ("what we think about": signals + slot contributions). They must not share `reasoning_artifact`; the validator treats overlap as an error. Don't put reasoning stages inside `task_template` (R8/TS9 violation) — that's the methodology's job, applied as a wrapper around each leaf-task execution.

**Two methodology packs ship today.** `process.lean_jtbd` is decision-style (goal → JTBD → options → decision) and applies to ТЗ-style flows. `process.descriptive_decomposition` is descriptive-style (scope → entities → relationships → completeness check) and applies to architecture-style flows. `project_service.init_project` picks the default by `objective_ref.identifier` prefix (`architecture.*` → descriptive, everything else → lean_jtbd). One active methodology per project; the existing `set_methodology` command can override.

**Methodology `if:` rules** are evaluated by a hand-rolled AST whitelist in `application/methodology_rule_eval.py` — only literals, dotted name lookups, basic operators, and `len/count/max/min/sum/is_null`. Do not reach for `eval()` / `regex` / `exec` — they fail silently to `False`.

**Engagement model** has two orthogonal axes (`application/clarification_service.py::_decide_action`):
- *Frequency*: `clarification_mode` + `min_participation_mode` ∈ {autopilot, balanced, control, expert}.
- *Authority*: `decision_owner_role` ∈ {business, client, methodologist, architect, data_owner, security}, with a per-role floor.

Decision order: high-confidence + `default_assumption` → assume; else if mode ≥ role floor → ask; else if no `default_assumption` → ask anyway; else assume.

**UI pyramid L1→L4** (`ui/workspace/src/`) — managers land on L1 Mission Control; technical detail (reasoning, provenance) is opt-in via drill-down. L3/L4 share `/api/projects/:id/tasks/:taskId/methodology-trace`. Spec: `specs/10_ui_workspace.md`.

**Provider switching** lives in `execution_service.execute_task` (switch on `active_provider`). The canonical path is the `infrastructure/llm/` package: `protocol.py` defines the `runtime_checkable` `LLMProvider` Protocol (any object with `chat_json(system, user, schema) -> dict` qualifies), `providers/` holds implementations, and `registry.py` resolves them from encrypted settings via `_PROVIDER_BUILDERS` + connection-type mapping. The flat `infrastructure/<name>_client.py` files are the older direct path still branched on for explicit provider names — `execution_service` comments call it "legacy". Adding a provider = new `infrastructure/llm/providers/<name>.py` + registration in `registry.py`, and optionally branching `clarification_service._build_draft` (CE11).

**Schema conformance is a 4-layer guarantee chain** (each layer is a no-op when the previous one did its job; full matrix in `infrastructure/llm/structured_output.py`): (1) the provider itself enforces the schema with the strongest native mechanism — openrouter sends `response_format: json_schema (strict)` with the schema losslessly transformed to the OpenAI strict subset (degrades json_schema → json_object → prompt-only on HTTP 400), claude_sdk forces tool use, claude_subscription passes `--json-schema` via the Agent SDK (`ResultMessage.structured_output`; schema stripped of descriptions to fit the Windows CLI-arg limit; degrades to schema-in-prompt on any structured-mode error); (2) `normalize_to_schema` deterministically repairs mechanical shape drift; (3) `_repair_payload_to_schema` fixes residual semantic gaps with a targeted LLM call on failing fields only; (4) strict validation remains the final gate. When touching providers, keep this contract: a provider must try to return schema-conforming JSON, not just any JSON.

## Conventions to respect

- **Artifact immutability (EC4)**: never mutate a created artifact. A correction is a new artifact.
- **No direct LLM → user questions (CE1)**: clarifications go through `ClarificationCandidate`. If you want a fallback answer, put it on `default_assumption`, not hard-coded in Python.
- **No legacy terms**: `recipe` / `recipe_fragment` were removed. Use `task_template` / `methodology_pack`.
- **Final task is closed for edits**: `requirements_spec_generation` uses `collect_optional.from_active_domain_packs: true` (Stage 7.3). New domain artifacts get pulled in automatically — don't hand-list them.
- **Unstructured contracts are explicit**: `additionalProperties: true` without required fields is only legal if the contract is also marked `unstructured: true` (Stage 7.5).

## Mermaid in PDF (optional)

UI renders Mermaid diagrams via `mermaid.js` in the browser. The PDF pipeline (`application/pdf_export.py`) uses `xhtml2pdf` which has no JS — so for graphical diagrams in PDF we preprocess each ```mermaid``` block via `mermaid_render`, which shells out to `mmdc` (`@mermaid-js/mermaid-cli`, Node + headless Chromium). **Format priority: SVG → PNG → code-block.** `render_mermaid_to_svg` renders vector SVG (with `htmlLabels:false` so labels are real `<text>`, not foreignObject), strips zero-length `stroke-dasharray` (which crashes reportlab), and only returns the SVG if it converts cleanly via `svglib`→`reportlab` (the same path `xhtml2pdf` uses) — so a pathological diagram can never crash PDF export; it falls back to `render_mermaid_to_png` (raster, `-s 2`), then to the original code-block. All inlined via data-URI. If `mmdc` is missing or the call fails, the code-block is left intact — degraded but never broken. Tests set `POV_MERMAID_DISABLED=1` to short-circuit.

**Setup is reproducible — no manual global install.** `@mermaid-js/mermaid-cli` + its peer `puppeteer` are declared as `optionalDependencies` of `ui/workspace`, so the standard `npm ci` installs `mmdc` into `ui/workspace/node_modules/.bin`. `mermaid_render._resolve_binary` resolves in this order: `POV_MERMAID_CLI` env override → project-local `node_modules/.bin/mmdc` (only when `puppeteer` is present, so a partial install never shadows a working one) → global `mmdc` on PATH (`shutil.which` resolves `mmdc.cmd` on Windows; `_build_command` wraps `.cmd`/`.bat` in `cmd /c`). Optional by design: if Chromium can't download, `npm ci` still succeeds and the PDF degrades to the code-block. Env knobs: `POV_MERMAID_CLI` (explicit binary path), `POV_MERMAID_TIMEOUT` (seconds, default 30), `POV_MERMAID_DISABLED`.

## LLM provider settings

Settings live in `<runtime>/settings.db` (Fernet-encrypted via `POV_SECRET_KEY` or auto-generated `<runtime>/.secret_key`), managed from the UI's `/settings` page. `.env` is only for bootstrap (first-run import) and CI/dev fallback. Three provider types: `openrouter`, `claude_sdk` (direct Anthropic API), `claude_subscription` (local `claude` CLI — needs `claude login` once; the bundled CLI from `claude-agent-sdk` is **not** used because it isn't logged in). Default execution provider is `stub` (deterministic fixtures from `templates/stub_fixtures/`, no network) — keep it that way for tests.

## Dependency notes

`starlette` is pinned `>=0.40,<0.42` on purpose: `claude-agent-sdk → mcp` transitively pulls `starlette 1.0`, which breaks `fastapi 0.115`. Regenerate the lockfile via `uv pip compile pyproject.toml --extra dev -o requirements.lock`.
