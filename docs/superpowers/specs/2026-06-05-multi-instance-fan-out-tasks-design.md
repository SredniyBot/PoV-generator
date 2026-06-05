# Multi-Instance Fan-Out Tasks — Design Spec

**Date:** 2026-06-05  
**Status:** Draft

---

## Overview

Add the ability for a task to be "multi-instance": a single task template that is executed once per element of a collection extracted from a completed upstream artifact. Instances are created lazily (dynamic fan-out) when the producer task completes.

**Key decisions:**
- Fan-out is a first-class `template_type: "fan_out"` — not an extension of composite
- Collection source is always an artifact from a preceding task (not config or user input)
- Each instance receives a structured object (item) from the array, not a flat string
- Each instance can itself be a composite, expanding into a full subtree
- A wrapper fan-out node waits for all N instances to complete before transitioning to `completed`
- UI is adaptive: ≤ 4 instances auto-expanded, > 4 collapsed by default

---

## 1. Domain Model

### 1.1 TaskStatus — new value

```python
TaskStatus = Literal[
    "candidate",
    "ready",
    "blocked",
    "in_progress",
    "waiting_for_children",
    "waiting_for_fan_out_source",   # NEW: fan-out wrapper waiting for producer artifact
    "completed",
    "failed",
    "skipped",
    "obsolete",
]
```

`waiting_for_fan_out_source` — the fan-out wrapper exists in the graph but the producer artifact is not yet ready. The planner ignores this node until the producer task completes.

### 1.2 State machine for fan-out node

```
initial_task_status("fan_out") → "waiting_for_fan_out_source"

waiting_for_fan_out_source  →  waiting_for_children   (command: expand_fan_out)
waiting_for_fan_out_source  →  failed                 (command: fail)
waiting_for_children        →  completed              (all children completed — existing logic)
waiting_for_children        →  failed                 (command: fail)
```

New command `expand_fan_out` is issued by the planner (not by the user) when the producer artifact becomes available. It transitions the wrapper and creates N child task records.

### 1.3 TaskRecord — no changes

`fan_out_spec` lives in the template (registry), not in the task record. `TaskRecord` is unchanged. The fan-out wrapper is identified solely by `template_type == "fan_out"` and its status.

Child instances created by fan-out are regular `TaskRecord` entries with:
- `parent_task_id = fan_out_wrapper.task_id`
- `stable_key = f"{fan_out_template_id}:{item[key_field]}"`
- `template_type` from `children_template_ref` (leaf or composite)

---

## 2. Registry / YAML Schema

### 2.1 New dataclasses in `domain/registry.py`

```python
@dataclass(frozen=True)
class FanOutSpec:
    artifact_role: str       # artifact_role of the producer task
    array_path: str          # dot-path to array inside the artifact (e.g. "competitors")
    key_field: str           # field used to build stable_key (e.g. "id")
    label_field: str         # field used for UI display (e.g. "name")

@dataclass(frozen=True)
class TaskTemplate:
    # existing fields ...
    template_type: TemplateType          # "composite" | "leaf" | "fan_out"
    children: tuple[TaskChildSpec, ...]
    slots: tuple[TaskSlotSpec, ...]
    fan_out_spec: FanOutSpec | None = None         # required when template_type == "fan_out"
    children_template_ref: str | None = None       # required when template_type == "fan_out"
```

`TemplateType` is extended to `Literal["composite", "leaf", "fan_out"]`.

### 2.2 Example YAML template

```yaml
id: analyze_competitors
version: "1.0"
kind: task_template
template_type: fan_out

fan_out_spec:
  artifact_role: competitor_list
  array_path: competitors
  key_field: id
  label_field: name

children_template_ref: analyze_single_competitor@1.0
```

`children_template_ref` may point to a `leaf` or `composite` template. When it points to a composite, the full subtree is recursively expanded for each instance.

### 2.3 Registry validator rules

Three new rules added to `registry validate`:

1. `fan_out_spec` is required when `template_type == "fan_out"`, forbidden otherwise.
2. `children_template_ref` is required when `template_type == "fan_out"`, forbidden otherwise.
3. `children_template_ref` must resolve to an existing template in the registry.

---

## 3. Planning & Execution Flow

### 3.1 Fan-out resolution step in PlanningService

`PlanningService.plan()` gains a new step **fan_out_resolution**, executed after each task completion:

```
task.status → completed
    ↓
plan()
    ├─ [existing] advance composite parents if all children done
    ├─ [existing] admit candidates → ready
    └─ [NEW] fan_out_resolution(completed_task_id)
            1. find all fan-out nodes in status waiting_for_fan_out_source
               whose fan_out_spec.artifact_role matches completed task's produced artifact_role
            2. read artifact from sqlite_runtime by artifact_role
            3. extract array: artifact.content[array_path]
            4. for each item in array:
                   stable_key = f"{fan_out_node.stable_key}:{item[key_field]}"
                   if stable_key already exists → skip (idempotent)
                   create TaskRecord(
                       template_type = resolve(children_template_ref).template_type,
                       parent_task_id = fan_out_node.task_id,
                       stable_key = stable_key,
                       ...
                   )
                   if children_template_ref is composite → recursively expand subtree
            5. apply_task_command(fan_out_node, "expand_fan_out")
               → status: waiting_for_fan_out_source → waiting_for_children
```

**Idempotency:** `stable_key` uniqueness prevents duplicate instance creation on replanning.

### 3.2 Empty array

If `array_path` resolves to an empty list, the fan-out wrapper transitions directly to `completed` (zero children = nothing to wait for). `status_summary` is set to `"Source empty, 0 instances created"`.

### 3.3 Producer retry

If the producer task is retried and overwrites its artifact, a new command `reset_fan_out` is applied to the existing fan-out wrapper (analogous to `retry` for leaf tasks):

1. All child instances of the wrapper transition to `obsolete`.
2. The wrapper's `attempt` counter is incremented; its status resets to `waiting_for_fan_out_source`.
3. On next `plan()`, fan-out resolution runs again against the new artifact and creates fresh instances (new `stable_key`s include the updated `attempt` suffix: `f"{fan_out_node.stable_key}:{item[key_field]}:{attempt}"`).

This preserves `stable_key` uniqueness while keeping the wrapper record identity stable.

---

## 4. Context Injection — FanOutItemContext

Each fan-out instance receives its item via a `fan_out_item` slot in the `ContextManifest`:

```python
@dataclass(frozen=True)
class FanOutItemContext:
    item: dict        # full item object from the array
    item_key: str     # value of key_field (e.g. "acme_corp")
    item_label: str   # value of label_field (e.g. "Acme Corp")
    total_count: int  # total number of instances
    index: int        # 0-based position in the array
```

This is added to `ContextManifest` alongside existing slots. No changes to the LLM call protocol.

The system prompt template for the child template may reference:
```
Processing item {{index + 1}} of {{total_count}}: "{{item_label}}"
Item data: {{item | json}}
```

### Artifact naming for instances

Each instance produces artifacts with a namespaced role:
```
artifact_role = f"{base_role}:{item_key}"
# e.g. "competitor_analysis:acme_corp"
```

Downstream tasks can reference a specific instance by exact role, or aggregate all via pattern `competitor_analysis:*` (requires glob support in `ContextManifest` — out of scope for this spec, noted as follow-up).

---

## 5. API & Query Layer

### TaskNodeView extension

`workspace_query_service.py` enriches `TaskNodeView` with:

```python
@dataclass(frozen=True)
class FanOutMeta:
    source_artifact_role: str
    total_instances: int
    completed_instances: int
    is_expanded: bool          # server-side: always True after expand_fan_out fires

@dataclass(frozen=True)
class TaskNodeView:
    # existing fields ...
    fan_out_meta: FanOutMeta | None  # non-None only when template_type == "fan_out"
```

The API response passes `fan_out_meta` through unchanged. No new endpoints needed.

---

## 6. UI — TaskGraphCanvas

### 6.1 New node type: FanOutCardNode

A dedicated React component alongside the existing `TaskCardNode`.

**Before producer completes (`waiting_for_fan_out_source`):**
```
┌─────────────────────────────────────────┐
│ ⚡ Анализ конкурентов          [fan-out] │
│  ○ Ожидает данных                        │
│  Источник: competitor_list              │
└─────────────────────────────────────────┘
```

**After expand (`waiting_for_children`):**
```
┌─────────────────────────────────────────┐
│ ⚡ Анализ конкурентов          [fan-out] │
│  ● В процессе                    3 / 10 │
│  ━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░         │
└─────────────────────────────────────────┘
```

### 6.2 Status → UI label mapping

| Domain status | UI label | Color |
|---|---|---|
| `waiting_for_fan_out_source` | Ожидает данных | gray, dashed border |
| `waiting_for_children` | В процессе (N/M) | blue |
| `completed` | Завершено | green |
| `failed` | Ошибка | red |

### 6.3 Adaptive instance display

- **≤ 4 instances** — auto-expanded; each instance rendered as a separate node/subtree
- **> 4 instances** — collapsed by default under the fan-out wrapper node; a "Show all N" button expands
- Collapsed/expanded state is local React state (not persisted to server)
- **Auto-pan behavior:** if the current task is inside a collapsed fan-out group, the group expands first before panning

### 6.4 Edge styling

- Edge from producer task → fan-out wrapper: **dashed**, color `#8B5CF6` (purple, data dependency)
- Edge from fan-out wrapper → instances: solid gray (standard parent-child)

The producer→fan-out edge is synthetic: it is not stored as a parent-child relationship but inferred from `fan_out_spec.artifact_role` matching the producer's produced artifact role.

---

## 7. Files Changed

| File | Change |
|---|---|
| `src/pov_generator/domain/tasks.py` | Add `waiting_for_fan_out_source` to `TaskStatus`; add `expand_fan_out` command |
| `src/pov_generator/domain/registry.py` | Add `FanOutSpec` dataclass; extend `TaskTemplate` with `fan_out_spec` + `children_template_ref`; add `"fan_out"` to `TemplateType` |
| `src/pov_generator/application/planning_service.py` | Add `fan_out_resolution()` step in `plan()` |
| `src/pov_generator/application/workspace_query_service.py` | Add `FanOutMeta`; enrich `TaskNodeView` with `fan_out_meta`; build synthetic producer→fan-out edges |
| `src/pov_generator/application/context_manifest.py` | Add `FanOutItemContext` to manifest construction |
| `src/pov_generator/infrastructure/sqlite_runtime.py` | Add `get_artifact_by_role()` helper for fan-out resolution |
| `src/pov_generator/interfaces/api.py` | Pass `fan_out_meta` through in task graph response |
| `ui/workspace/src/TaskGraphCanvas.tsx` | Add `FanOutCardNode`; adaptive collapse/expand; dashed producer edge; auto-pan through collapsed groups |
| `templates/` | New YAML templates using `template_type: fan_out` |
| `tests/test_foundation.py` | Update registry validation test for new fan-out rules |

---

## 8. Out of Scope

- Glob pattern resolution for `artifact_role` in `ContextManifest` (`competitor_analysis:*`)
- User-initiated fan-out (manually specifying the collection from UI)
- Fan-out over config/domain-pack data (Approach B patterns)
- Aggregation tasks that consume all N instance artifacts as inputs
