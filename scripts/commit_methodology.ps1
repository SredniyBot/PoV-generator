# Тематические коммиты по фазам (spec + phases 0–8) на ветке spec/methodology-pack-v2.1.
# Перед запуском убедись, что:
#   1) ты на нужной ветке (`git branch --show-current`)
#   2) `.git/index.lock` отсутствует (`Remove-Item .git\index.lock` если висит)
#
# Скрипт делает 6 тематических коммитов. Если хочешь один большой —
# в конце есть закомментированный fallback.

$ErrorActionPreference = "Stop"

if (Test-Path .git\index.lock) {
    Remove-Item .git\index.lock -Force
}

# 1. Спецификации и JSON Schema
git add specs README.md
git commit -m "spec: methodology pack v2.1, UI pyramid drill-down, typed gates

- New kind 'methodology_pack' as 7th registry kind (02_registry_dsl, schemas).
- task_template simplified: 'complexity' enum, methodology fields removed.
- quality_gate typed: human_approval / external_signoff / automated_review.
- ProblemState gains active_methodology_packs (05).
- ExecutionRequest/Result extended for complexity, methodology_pack_ref,
  three output artifacts: primary + reasoning + methodology_trace (07).
- UI iyl pyramid drill-down L1-L4 (10), project_overview/methodology
  projections (11).
- Clarification source_type += methodology_pack (12).
- Vision: After-MVP roadmap (per-stage CoT, multi-active methodology,
  dynamic complexity, objective chains).
- README: section about methodology, complexity, claude providers."

# 2. Контракты реестра + первый методологический пак
git add `
  src/pov_generator/domain/registry.py `
  src/pov_generator/infrastructure/filesystem_registry.py `
  src/pov_generator/application/registry_service.py `
  templates/methodologies/process.lean_jtbd.yaml `
  templates/gates/common/requirements_spec_review_passed.yaml
git commit -m "feat(registry): methodology_pack kind, complexity, typed gates

- MethodologyPackSpec / MethodologyStageSpec / MethodologyStageRule etc.
- TemplateSpec.complexity (trivial|standard|complex).
- QualityGateSpec extended (approver_role, decision_modes, blocking,
  timeout_hours, validator_ref, on_pass, on_comments).
- parse_methodology_pack + filesystem loader for templates/methodologies.
- registry validate(): structural checks for methodology stages and refs.
- First pack: process.lean_jtbd@1.0.0 (goal_framing -> jtbd_anchor ->
  option_generation -> decision; rules ambiguous_choice, low_overall_confidence;
  trivial-complexity skips option_generation).
- Existing gate common.requirements_spec_review_passed migrated to
  automated_review (legacy llm_review alias still accepted)."

# 3. ProblemState активация + execution wrapper + Claude SDK + подписка + правила + gate
git add `
  src/pov_generator/domain/problem_state.py `
  src/pov_generator/domain/artifacts.py `
  src/pov_generator/domain/execution.py `
  src/pov_generator/domain/clarifications.py `
  src/pov_generator/infrastructure/sqlite_runtime.py `
  src/pov_generator/infrastructure/claude_sdk_client.py `
  src/pov_generator/infrastructure/claude_subscription_client.py `
  src/pov_generator/application/project_service.py `
  src/pov_generator/application/execution_service.py `
  src/pov_generator/application/validation_service.py `
  src/pov_generator/application/planning_service.py `
  pyproject.toml
git commit -m "feat(state+execution): active methodology, wrapper, Claude SDK + Claude subscription

- ProblemState.active_methodology_packs + activate/disable patches.
- ProjectService.init_project auto-activates default 'process.lean_jtbd@1.0.0';
  ProjectService.set_methodology with replace.
- SqliteRuntime serializes new field for backward compatibility.
- ExecutionRequest gains complexity + methodology_pack_ref;
  ExecutionResult outputs become tuple of primary/reasoning/trace.
- ArtifactKind = primary|reasoning|trace|derived.
- ClarificationSourceType += methodology_pack.
- ExecutionProvider += claude_sdk, claude_subscription.
- Methodology wrapper in execution_service: combined LLM call with
  primary+reasoning structured schema (single_call mode), stub keeps
  deterministic mock outputs; Claude SDK provider via Anthropic SDK
  with tool-use, Claude subscription provider via claude-agent-sdk
  (no API key, uses local 'claude' CLI session).
- ValidationService: methodology rule evaluator (empty_goal,
  ambiguous_choice, low_overall_confidence) emits ClarificationCandidate
  with source_type=methodology_pack; gate-candidate emission for
  human_approval gates after review_report.
- PlanningService._objective_completed enforces human_approval gates.
- pyproject: anthropic, claude-agent-sdk."

# 4. Workspace projections + API + UI
git add `
  src/pov_generator/application/workspace_command_service.py `
  src/pov_generator/application/workspace_query_service.py `
  src/pov_generator/domain/workspace_views.py `
  src/pov_generator/interfaces/api.py `
  ui/workspace/src/types.ts `
  ui/workspace/src/api.ts `
  ui/workspace/src/App.tsx
git commit -m "feat(api+ui): overview, methodology and provenance projections + UI

- ProjectOverviewView + project_overview() in query_service:
  stage_summary, current_activity, objective_progress, top critical
  clarifications, key artifacts, active methodology / domain packs.
- list_methodology_packs() returns registered packs with stages/rules.
- task_methodology_trace(project_id, task_id) for L4 provenance.
- New REST endpoints:
  GET /api/projects/{id}/overview
  GET /api/registry/methodology-packs
  GET /api/projects/{id}/tasks/{task_id}/methodology-trace
  POST /api/projects/{id}/commands/set-methodology
- UI types.ts: ProjectOverviewView, OverviewClarificationItem,
  OverviewArtifactItem, ObjectiveProgressView; ProjectStateView gains
  active_methodology_packs.
- UI api.ts: getOverview, listMethodologyPacks, getMethodologyTrace,
  setMethodology.
- UI App.tsx: MethodologyOverviewSection in OverviewPage shows
  stage_summary, progress, methodology, critical clarifications."

# 5. Тесты
git add tests/test_foundation.py
git commit -m "test: regression tests for methodology, overview and provenance

- registry: methodology pack + stages_for_complexity + legacy gate alias.
- ProblemState: default methodology activated on init; set_methodology.
- ExecutionService: three artifacts (primary/reasoning/trace) per leaf run.
- ProjectOverviewView exposes methodology and progress.
- task_methodology_trace returns reasoning + trace artifacts."

# 6. UI dist / lockfile / другие сопутствующие — если есть
$remaining = git status --porcelain
if ($remaining) {
    git add -A
    git commit -m "chore: misc updates from methodology rollout"
}

git log --oneline -8

# === Fallback (один большой коммит вместо тематических) ===
# git add -A
# git commit -m "feat: methodology pack as first-class kind, claude providers, mission-control overview, provenance L4 (phases 0-8)"
