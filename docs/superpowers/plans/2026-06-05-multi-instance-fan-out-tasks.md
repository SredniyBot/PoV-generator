# Multi-Instance Fan-Out Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `fan_out` task type that lazily creates N child task instances from an array in a completed upstream artifact.

**Architecture:** New `template_type: "fan_out"` in the registry carries a `FanOutSpec` declaring which artifact to read and how to extract items. When the producer artifact becomes available, `_expand_fan_outs()` in `PlanningService` creates child tasks and transitions the wrapper to `waiting_for_children`. Fan-out nodes complete the same way composites do — when all children are done.

**Tech Stack:** Python 3.11+, dataclasses (frozen), SQLite via `SqliteRuntime`, ReactFlow + dagre (TypeScript), pytest.

**Spec:** `docs/superpowers/specs/2026-06-05-multi-instance-fan-out-tasks-design.md`

---

## File Map

| File | Change |
|---|---|
| `src/pov_generator/domain/tasks.py` | +status `waiting_for_fan_out_source`; +commands `expand_fan_out`, `reset_fan_out`; +origin kind `fan_out_instance`; update `initial_task_status` |
| `src/pov_generator/domain/registry.py` | +`FanOutSpec` dataclass; extend `TemplateSpec` with `fan_out_spec` + `children_template_ref`; add `"fan_out"` to `TemplateType`; update `parse_task_template` |
| `src/pov_generator/application/planning_service.py` | +`_expand_fan_outs()`; call it from `expand_graph()`; update `_refresh_composite_completion_from_tasks` to handle fan-out nodes |
| `src/pov_generator/domain/workspace_views.py` | +`FanOutMeta` dataclass; add `fan_out_meta` field to `TaskNodeView` |
| `src/pov_generator/application/workspace_query_service.py` | pass `context.snapshot` to `_build_task_tree`; populate `fan_out_meta` |
| `ui/workspace/src/TaskGraphCanvas.tsx` | +`fan_out_meta` to `TaskNodeView` interface; +`FanOutCardNode`; adaptive collapse; dashed producer edge |
| `templates/fan_out_example/sample_fan_out.yaml` | example `fan_out` template for registry tests |
| `tests/test_fan_out_tasks.py` | unit tests for domain layer |
| `tests/test_fan_out_planning.py` | integration tests for planning layer |
| `tests/test_foundation.py` | update registry counts; add fan_out validator rules test |

---

## Task 1: Domain — TaskStatus, TaskCommand, TaskOriginKind, state machine

**Files:**
- Modify: `src/pov_generator/domain/tasks.py`
- Create: `tests/test_fan_out_tasks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fan_out_tasks.py
from __future__ import annotations
import pytest
from pov_generator.domain.tasks import (
    TaskRecord,
    apply_task_command,
    initial_task_status,
)
from pov_generator.common.errors import ConflictError
from pov_generator.common.serialization import utc_now_iso


def _make_task(**overrides) -> TaskRecord:
    now = utc_now_iso()
    base = dict(
        task_id="t1",
        project_id="p1",
        objective_ref="obj@1.0.0",
        parent_task_id=None,
        template_ref="tmpl@1.0.0",
        template_type="fan_out",
        title="Test fan-out",
        status="waiting_for_fan_out_source",
        origin_kind="fan_out_instance",
        origin_ref="item_key",
        stable_key="sk",
        depth=0,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return TaskRecord(**base)


def test_initial_status_fan_out():
    assert initial_task_status("fan_out") == "waiting_for_fan_out_source"


def test_expand_fan_out_transitions_to_waiting_for_children():
    task = _make_task(status="waiting_for_fan_out_source")
    result = apply_task_command(task, "expand_fan_out")
    assert result.status == "waiting_for_children"


def test_expand_fan_out_rejects_wrong_status():
    task = _make_task(status="failed")
    with pytest.raises(ConflictError):
        apply_task_command(task, "expand_fan_out")


def test_reset_fan_out_increments_attempt_and_resets_status():
    task = _make_task(status="waiting_for_children", attempt=1)
    result = apply_task_command(task, "reset_fan_out")
    assert result.status == "waiting_for_fan_out_source"
    assert result.attempt == 2
    assert result.error_message is None


def test_reset_fan_out_rejects_completed():
    task = _make_task(status="completed")
    with pytest.raises(ConflictError):
        apply_task_command(task, "reset_fan_out")
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/test_fan_out_tasks.py -q
```
Expected: `ERROR` — `fan_out_instance` is not a valid `TaskOriginKind`, `waiting_for_fan_out_source` not in `TaskStatus`.

- [ ] **Step 3: Implement — tasks.py**

In `src/pov_generator/domain/tasks.py`:

Replace line 10–20 (TaskStatus):
```python
TaskStatus = Literal[
    "candidate",
    "ready",
    "blocked",
    "in_progress",
    "waiting_for_children",
    "waiting_for_fan_out_source",
    "completed",
    "failed",
    "skipped",
    "obsolete",
]
```

Replace line 21–23 (TaskCommand):
```python
TaskCommand = Literal[
    "start", "complete", "fail", "retry", "obsolete", "skip",
    "mark_ready", "mark_blocked", "cancel",
    "expand_fan_out", "reset_fan_out",
]
```

Replace line 24 (TaskOriginKind):
```python
TaskOriginKind = Literal[
    "objective_root", "base_child", "domain_contribution",
    "repair", "user_request", "system", "fan_out_instance",
]
```

Replace lines 70–73 (`initial_task_status`):
```python
def initial_task_status(template_type: str) -> TaskStatus:
    if template_type == "composite":
        return "waiting_for_children"
    if template_type == "fan_out":
        return "waiting_for_fan_out_source"
    return "candidate"
```

Add two new branches inside `apply_task_command`, before the final `raise TypeError` (line 122):
```python
    if command == "expand_fan_out":
        if current != "waiting_for_fan_out_source":
            raise ConflictError(f"Cannot expand_fan_out task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "waiting_for_children", "error_message": None, "updated_at": now})
    if command == "reset_fan_out":
        if current not in {"waiting_for_fan_out_source", "waiting_for_children", "failed"}:
            raise ConflictError(f"Cannot reset_fan_out task from status '{current}'.")
        return TaskRecord(
            **{**task.__dict__, "status": "waiting_for_fan_out_source", "attempt": task.attempt + 1, "error_message": None, "updated_at": now}
        )
```

- [ ] **Step 4: Run to confirm PASS**

```bash
python -m pytest tests/test_fan_out_tasks.py -q
```
Expected: `5 passed`

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest -q
```
Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/pov_generator/domain/tasks.py tests/test_fan_out_tasks.py
git commit -m "feat(domain): add fan_out status, commands, and origin kind to task model"
```

---

## Task 2: Domain — FanOutSpec, TemplateType, TemplateSpec, parse_task_template

**Files:**
- Modify: `src/pov_generator/domain/registry.py`
- Create: `templates/fan_out_example/sample_fan_out.yaml`
- Modify: `tests/test_fan_out_tasks.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_fan_out_tasks.py`:
```python
from pathlib import Path
import tempfile, textwrap, yaml
from pov_generator.domain.registry import parse_task_template, FanOutSpec
from pov_generator.common.errors import ValidationError


def _template_raw_fan_out() -> dict:
    return {
        "id": "test.fan_out_template",
        "version": "1.0.0",
        "type": "fan_out",
        "title": "Fan-out template",
        "fan_out_spec": {
            "artifact_role": "competitor_list",
            "array_path": "competitors",
            "key_field": "id",
            "label_field": "name",
        },
        "children_template_ref": "test.child_template@1.0.0",
        "children": [],
        "slots": [],
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }


def test_parse_fan_out_template_produces_fan_out_spec():
    raw = _template_raw_fan_out()
    tmpl = parse_task_template(raw, Path("test.yaml"))
    assert tmpl.template_type == "fan_out"
    assert isinstance(tmpl.fan_out_spec, FanOutSpec)
    assert tmpl.fan_out_spec.artifact_role == "competitor_list"
    assert tmpl.fan_out_spec.array_path == "competitors"
    assert tmpl.fan_out_spec.key_field == "id"
    assert tmpl.fan_out_spec.label_field == "name"
    assert tmpl.children_template_ref == "test.child_template@1.0.0"


def test_parse_fan_out_template_missing_fan_out_spec_raises():
    raw = _template_raw_fan_out()
    del raw["fan_out_spec"]
    with pytest.raises(ValidationError, match="fan_out_spec"):
        parse_task_template(raw, Path("test.yaml"))


def test_parse_fan_out_template_missing_children_template_ref_raises():
    raw = _template_raw_fan_out()
    del raw["children_template_ref"]
    with pytest.raises(ValidationError, match="children_template_ref"):
        parse_task_template(raw, Path("test.yaml"))


def test_parse_non_fan_out_template_with_fan_out_spec_raises():
    raw = _template_raw_fan_out()
    raw["type"] = "leaf"
    with pytest.raises(ValidationError, match="fan_out_spec"):
        parse_task_template(raw, Path("test.yaml"))
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/test_fan_out_tasks.py::test_parse_fan_out_template_produces_fan_out_spec -q
```
Expected: `ERROR` or `FAIL` — `FanOutSpec` does not exist yet, `"fan_out"` not accepted.

- [ ] **Step 3: Add FanOutSpec dataclass to registry.py**

In `src/pov_generator/domain/registry.py`, replace line 9:
```python
TemplateType = Literal["composite", "leaf", "fan_out"]
```

After line 87 (`class TaskSlotSpec`), insert new dataclass:
```python
@dataclass(frozen=True)
class FanOutSpec:
    artifact_role: str   # artifact_role of the producer task
    array_path: str      # dot-separated path to array in artifact JSON (e.g. "competitors")
    key_field: str       # item field used to build stable_key
    label_field: str     # item field used for UI display
```

In `TemplateSpec` dataclass (around line 229), add two new optional fields at the end, before `source_path`:
```python
    fan_out_spec: FanOutSpec | None = None
    children_template_ref: str | None = None
    source_path: Path = Path("")
```
(Remove the existing `source_path: Path = Path("")` line and re-add it after the two new fields.)

- [ ] **Step 4: Update parse_task_template to handle "fan_out"**

In `src/pov_generator/domain/registry.py`, replace lines 660–662:
```python
    template_type = require_str(raw, "type", owner)
    if template_type not in {"composite", "leaf", "fan_out"}:
        raise ValidationError(f"Unsupported task template type '{template_type}' in {owner}")
```

After the `merge_config` block (around line 735), before the `return TemplateSpec(...)` call, insert:
```python
    # fan_out fields
    fan_out_spec: FanOutSpec | None = None
    children_template_ref_val: str | None = None
    if template_type == "fan_out":
        fan_out_raw = raw.get("fan_out_spec")
        if not isinstance(fan_out_raw, dict):
            raise ValidationError(f"fan_out_spec is required for fan_out template in {owner}")
        fan_out_spec = FanOutSpec(
            artifact_role=require_str(fan_out_raw, "artifact_role", owner),
            array_path=require_str(fan_out_raw, "array_path", owner),
            key_field=require_str(fan_out_raw, "key_field", owner),
            label_field=require_str(fan_out_raw, "label_field", owner),
        )
        children_template_ref_val = raw.get("children_template_ref")
        if not isinstance(children_template_ref_val, str) or not children_template_ref_val:
            raise ValidationError(f"children_template_ref is required for fan_out template in {owner}")
    else:
        if "fan_out_spec" in raw:
            raise ValidationError(f"fan_out_spec is only allowed for fan_out templates in {owner}")
        if "children_template_ref" in raw:
            raise ValidationError(f"children_template_ref is only allowed for fan_out templates in {owner}")
```

At the end of `return TemplateSpec(...)` (before the closing parenthesis, around line 787–788), add:
```python
        fan_out_spec=fan_out_spec,
        children_template_ref=children_template_ref_val,
```

Also remove the `# type: ignore[arg-type]` comment on the `template_type=template_type` line since `TemplateType` now includes `"fan_out"`.

- [ ] **Step 5: Run to confirm PASS**

```bash
python -m pytest tests/test_fan_out_tasks.py -q
```
Expected: `9 passed`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest -q
```
Expected: all previously passing tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/pov_generator/domain/registry.py tests/test_fan_out_tasks.py
git commit -m "feat(domain): add FanOutSpec to registry; extend TemplateSpec with fan_out support"
```

---

## Task 3: Planning — _expand_fan_outs + completion for fan-out nodes

**Files:**
- Modify: `src/pov_generator/application/planning_service.py`
- Create: `tests/test_fan_out_planning.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fan_out_planning.py
from __future__ import annotations
import json, shutil
from pathlib import Path

import pytest
import yaml

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactRecord, ArtifactFormat, ArtifactKind
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_ROOT = REPO_ROOT / "templates"


def _build_services(registry_root: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    return registry_service, runtime, project_service, planning_service


def _inject_fan_out_template(templates_root: Path) -> None:
    """Add a minimal fan_out template and a matching leaf child template to registry."""
    fan_out_dir = templates_root / "tasks" / "test_fan_out"
    fan_out_dir.mkdir(parents=True, exist_ok=True)

    # Child leaf template
    (fan_out_dir / "test_fan_out_child.yaml").write_text(yaml.dump({
        "id": "test.fan_out_child",
        "version": "1.0.0",
        "kind": "task_template",
        "type": "leaf",
        "title": "Process single item",
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }, allow_unicode=True), encoding="utf-8")

    # Fan-out wrapper template
    (fan_out_dir / "test_fan_out_wrapper.yaml").write_text(yaml.dump({
        "id": "test.fan_out_wrapper",
        "version": "1.0.0",
        "kind": "task_template",
        "type": "fan_out",
        "title": "Fan-out over items",
        "fan_out_spec": {
            "artifact_role": "item_list",
            "array_path": "items",
            "key_field": "id",
            "label_field": "name",
        },
        "children_template_ref": "test.fan_out_child@1.0.0",
        "children": [],
        "slots": [],
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }, allow_unicode=True), encoding="utf-8")


def _make_fan_out_workspace(tmp_path: Path) -> tuple:
    registry_root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT, registry_root)
    _inject_fan_out_template(registry_root)
    registry_service, runtime, project_service, planning_service = _build_services(registry_root)
    snapshot, report = registry_service.validate()
    assert report.is_valid, [str(e) for e in report.errors]
    workspace = tmp_path / "ws"
    project_service.init_project(
        workspace=workspace,
        name="Test",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Test request",
        domain_packs=(),
    )
    return workspace, snapshot, runtime, planning_service


def _store_item_list_artifact(runtime: SqliteRuntime, workspace: Path, items: list[dict]) -> None:
    """Store a fake artifact with artifact_role='item_list' containing an items array."""
    from pov_generator.domain.artifacts import ArtifactMetadata, ArtifactRelations
    artifact = ArtifactRecord(
        artifact_id="art-test-1",
        project_id="p1",
        artifact_role="item_list",
        title="Item list",
        description=None,
        artifact_format=ArtifactFormat.json,
        artifact_kind=ArtifactKind.primary,
        created_by_task_id=None,
        storage_path="artifacts/item_list.json",
        created_at=utc_now_iso(),
        relations=ArtifactRelations(),
        metadata=ArtifactMetadata(),
    )
    runtime.store_artifact(workspace, artifact=artifact, content=json.dumps({"items": items}))


def test_fan_out_node_starts_in_waiting_for_source(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)

    # Manually create a fan_out task to test initial status
    from pov_generator.domain.tasks import initial_task_status
    assert initial_task_status("fan_out") == "waiting_for_fan_out_source"


def test_expand_fan_outs_creates_instances_when_artifact_ready(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)

    # Create a fan_out task manually in the workspace
    from pov_generator.domain.tasks import TaskRecord
    now = utc_now_iso()
    fan_out_task = runtime.create_task(workspace, TaskRecord(
        task_id="fan-out-1",
        project_id=runtime.load_manifest(workspace).project_id,
        objective_ref="common.requirements_specification@1.0.0",
        parent_task_id=None,
        template_ref="test.fan_out_wrapper@1.0.0",
        template_type="fan_out",
        title="Fan-out over items",
        status="waiting_for_fan_out_source",
        origin_kind="base_child",
        origin_ref="test",
        stable_key="test:fan_out_wrapper",
        depth=1,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    ))

    # No artifact yet — no instances should be created
    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    fan_out_tasks = [t for t in tasks if t.task_id == fan_out_task.task_id]
    assert fan_out_tasks[0].status == "waiting_for_fan_out_source"
    assert not any(t.parent_task_id == fan_out_task.task_id for t in tasks)

    # Store the source artifact
    _store_item_list_artifact(runtime, workspace, [
        {"id": "item_a", "name": "Item A"},
        {"id": "item_b", "name": "Item B"},
    ])

    # Now expand — should create 2 child instances
    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    instances = [t for t in tasks if t.parent_task_id == fan_out_task.task_id]
    assert len(instances) == 2
    assert all(t.origin_kind == "fan_out_instance" for t in instances)
    # Fan-out wrapper should now be waiting_for_children
    updated_wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert updated_wrapper.status == "waiting_for_children"


def test_expand_fan_outs_is_idempotent(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    from pov_generator.domain.tasks import TaskRecord
    now = utc_now_iso()
    fan_out_task = runtime.create_task(workspace, TaskRecord(
        task_id="fan-out-2",
        project_id=runtime.load_manifest(workspace).project_id,
        objective_ref="common.requirements_specification@1.0.0",
        parent_task_id=None,
        template_ref="test.fan_out_wrapper@1.0.0",
        template_type="fan_out",
        title="Fan-out over items",
        status="waiting_for_fan_out_source",
        origin_kind="base_child",
        origin_ref="test",
        stable_key="test:fan_out_wrapper_idem",
        depth=1,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    ))
    _store_item_list_artifact(runtime, workspace, [{"id": "x", "name": "X"}])

    planning_service._expand_fan_outs(workspace, snapshot)
    count_after_first = len([t for t in runtime.list_tasks(workspace) if t.parent_task_id == fan_out_task.task_id])

    # Calling again should not duplicate instances
    planning_service._expand_fan_outs(workspace, snapshot)
    count_after_second = len([t for t in runtime.list_tasks(workspace) if t.parent_task_id == fan_out_task.task_id])

    assert count_after_first == count_after_second == 1


def test_fan_out_wrapper_completes_when_all_instances_done(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    from pov_generator.domain.tasks import TaskRecord
    now = utc_now_iso()
    project_id = runtime.load_manifest(workspace).project_id
    fan_out_task = runtime.create_task(workspace, TaskRecord(
        task_id="fan-out-3",
        project_id=project_id,
        objective_ref="common.requirements_specification@1.0.0",
        parent_task_id=None,
        template_ref="test.fan_out_wrapper@1.0.0",
        template_type="fan_out",
        title="Fan-out",
        status="waiting_for_fan_out_source",
        origin_kind="base_child",
        origin_ref="test",
        stable_key="test:fan_out_wrapper_complete",
        depth=1,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    ))
    _store_item_list_artifact(runtime, workspace, [{"id": "c1", "name": "C1"}])
    planning_service._expand_fan_outs(workspace, snapshot)

    # Complete the single instance
    tasks = runtime.list_tasks(workspace)
    instance = next(t for t in tasks if t.parent_task_id == fan_out_task.task_id)
    runtime.transition_task(workspace, instance.task_id, "complete")

    # Refresh completion — wrapper should complete
    planning_service._refresh_composite_completion(workspace)
    tasks = runtime.list_tasks(workspace)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "completed"
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/test_fan_out_planning.py -q
```
Expected: `AttributeError: 'PlanningService' object has no attribute '_expand_fan_outs'`

- [ ] **Step 3: Add _expand_fan_outs to planning_service.py**

In `src/pov_generator/application/planning_service.py`, add import at top:
```python
from ..common.serialization import json_loads, utc_now_iso
```
(Replace existing `from ..common.serialization import utc_now_iso` line.)

Add new method `_expand_fan_outs` right after `_expand_composites` (after line ~264):
```python
    def _expand_fan_outs(self, workspace: Path, snapshot: RegistrySnapshot) -> None:
        tasks = self._runtime.list_tasks(workspace)
        for task in tasks:
            if task.template_type != "fan_out" or task.status != "waiting_for_fan_out_source":
                continue
            template = snapshot.resolve_template(task.template_ref)
            if template.fan_out_spec is None or template.children_template_ref is None:
                continue
            artifact = self._runtime.latest_artifact_by_role(workspace, template.fan_out_spec.artifact_role)
            if artifact is None:
                continue
            content_str = self._runtime.load_artifact_content(workspace, artifact.artifact_id)
            content = json_loads(content_str)
            # Extract array via dot-path
            array: object = content
            for part in template.fan_out_spec.array_path.split("."):
                if not isinstance(array, dict):
                    array = []
                    break
                array = array.get(part, [])
            if not isinstance(array, list):
                array = []

            child_template = snapshot.resolve_template(template.children_template_ref)
            for idx, item in enumerate(array):
                item_key = str(item.get(template.fan_out_spec.key_field, idx)) if isinstance(item, dict) else str(idx)
                stable_key = f"{task.stable_key}:instance:{item_key}:{task.attempt}"
                if self._runtime.find_task_by_stable_key(workspace, stable_key) is not None:
                    continue
                self._create_task(
                    workspace,
                    project_id=task.project_id,
                    objective_ref=task.objective_ref,
                    parent_task_id=task.task_id,
                    template=child_template,
                    origin_kind="fan_out_instance",
                    origin_ref=item_key,
                    stable_key=stable_key,
                    depth=task.depth + 1,
                    slot_id=None,
                )
            self._runtime.transition_task(workspace, task.task_id, "expand_fan_out")
```

- [ ] **Step 4: Call _expand_fan_outs from expand_graph**

In `expand_graph` method (around line 67), after `self._expand_composites(workspace, snapshot)`:
```python
        self._expand_composites(workspace, snapshot)
        self._expand_fan_outs(workspace, snapshot)
        return tuple(self._runtime.list_tasks(workspace))
```

- [ ] **Step 5: Update _refresh_composite_completion_from_tasks to include fan_out**

In `_refresh_composite_completion_from_tasks` (around line 501), replace:
```python
            if task.template_type != "composite" or task.status == "completed":
```
with:
```python
            if task.template_type not in {"composite", "fan_out"} or task.status == "completed":
```

- [ ] **Step 6: Run to confirm PASS**

```bash
python -m pytest tests/test_fan_out_planning.py -q
```
Expected: `5 passed`

- [ ] **Step 7: Run full suite**

```bash
python -m pytest -q
```
Expected: all previously passing tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/pov_generator/application/planning_service.py tests/test_fan_out_planning.py
git commit -m "feat(planning): add _expand_fan_outs; fan_out nodes complete like composites"
```

---

## Task 4: Views — FanOutMeta + TaskNodeView enrichment

**Files:**
- Modify: `src/pov_generator/domain/workspace_views.py`
- Modify: `src/pov_generator/application/workspace_query_service.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_fan_out_planning.py`:
```python
from pov_generator.domain.workspace_views import FanOutMeta


def test_task_graph_view_has_fan_out_meta_for_fan_out_nodes(tmp_path: Path):
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    from pov_generator.domain.tasks import TaskRecord
    now = utc_now_iso()
    project_id = runtime.load_manifest(workspace).project_id
    runtime.create_task(workspace, TaskRecord(
        task_id="fan-out-view",
        project_id=project_id,
        objective_ref="common.requirements_specification@1.0.0",
        parent_task_id=None,
        template_ref="test.fan_out_wrapper@1.0.0",
        template_type="fan_out",
        title="Fan-out",
        status="waiting_for_fan_out_source",
        origin_kind="base_child",
        origin_ref="test",
        stable_key="test:fan_out_view",
        depth=1,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    ))

    query_service = WorkspaceQueryService(runtime, planning_service)
    # We call the internal helper directly to avoid needing a full project setup
    tasks = runtime.list_tasks(workspace)
    nodes = query_service._build_task_tree(workspace, tasks, None, snapshot)
    fan_out_nodes = [n for n in nodes if n.template_type == "fan_out"]
    assert len(fan_out_nodes) == 1
    assert fan_out_nodes[0].fan_out_meta is not None
    assert isinstance(fan_out_nodes[0].fan_out_meta, FanOutMeta)
    assert fan_out_nodes[0].fan_out_meta.source_artifact_role == "item_list"
    assert fan_out_nodes[0].fan_out_meta.total_instances == 0
    assert fan_out_nodes[0].fan_out_meta.completed_instances == 0
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/test_fan_out_planning.py::test_task_graph_view_has_fan_out_meta_for_fan_out_nodes -q
```
Expected: `AttributeError: module ... has no attribute 'FanOutMeta'`

- [ ] **Step 3: Add FanOutMeta to workspace_views.py**

In `src/pov_generator/domain/workspace_views.py`, after the existing imports and before `class TaskNodeView` (around line 78), insert:
```python
@dataclass(frozen=True)
class FanOutMeta:
    source_artifact_role: str
    total_instances: int
    completed_instances: int
    producer_task_id: str | None = None
```

In `TaskNodeView` dataclass, add field after `children` (end of class, around line 99):
```python
    fan_out_meta: "FanOutMeta | None" = None
```

- [ ] **Step 4: Update _build_task_tree signature and fan_out_meta population**

In `src/pov_generator/application/workspace_query_service.py`:

Add import at top (in the `from ..domain.workspace_views import (` block):
```python
    FanOutMeta,
```

Also add to imports from domain:
```python
from ..domain.registry import RegistrySnapshot
```
(Check if it's already imported — search for `RegistrySnapshot` in the file; if not present, add it.)

Change `_build_task_tree` signature from:
```python
    def _build_task_tree(self, workspace: Path, tasks: list[TaskRecord], current_task_id: str | None) -> tuple[TaskNodeView, ...]:
```
to:
```python
    def _build_task_tree(self, workspace: Path, tasks: list[TaskRecord], current_task_id: str | None, snapshot: RegistrySnapshot | None = None) -> tuple[TaskNodeView, ...]:
```

Inside `_build_task_tree`, after building `children_by_parent` (around line 1292), add:
```python
        # Index children by parent for fan_out_meta counts
        children_count_by_parent: dict[str, int] = {}
        completed_count_by_parent: dict[str, int] = {}
        for task in tasks:
            if task.parent_task_id:
                children_count_by_parent[task.parent_task_id] = children_count_by_parent.get(task.parent_task_id, 0) + 1
                if task.status == "completed":
                    completed_count_by_parent[task.parent_task_id] = completed_count_by_parent.get(task.parent_task_id, 0) + 1
```

Inside the `build(task)` function, add logic to build `fan_out_meta` before the `return TaskNodeView(...)`:
```python
            fan_out_meta = None
            if task.template_type == "fan_out" and snapshot is not None:
                try:
                    tmpl = snapshot.resolve_template(task.template_ref)
                    if tmpl.fan_out_spec is not None:
                        fan_out_meta = FanOutMeta(
                            source_artifact_role=tmpl.fan_out_spec.artifact_role,
                            total_instances=children_count_by_parent.get(task.task_id, 0),
                            completed_instances=completed_count_by_parent.get(task.task_id, 0),
                        )
                except Exception:
                    pass
```

Add `fan_out_meta=fan_out_meta,` to the `TaskNodeView(...)` constructor call.

Update the call site in `_build_task_graph` (around line 304):
```python
        nodes = self._build_task_tree(context.workspace, tasks, ready.task_id if ready else None, context.snapshot)
```

- [ ] **Step 5: Run to confirm PASS**

```bash
python -m pytest tests/test_fan_out_planning.py -q
```
Expected: `6 passed`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest -q
```
Expected: all previously passing tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/pov_generator/domain/workspace_views.py src/pov_generator/application/workspace_query_service.py tests/test_fan_out_planning.py
git commit -m "feat(views): add FanOutMeta to TaskNodeView; populate from snapshot in _build_task_tree"
```

---

## Task 5: UI — FanOutCardNode, adaptive collapse, dashed producer edge

**Files:**
- Modify: `ui/workspace/src/TaskGraphCanvas.tsx`

- [ ] **Step 1: Check TypeScript types compile cleanly before touching**

```bash
cd ui/workspace && npm run build 2>&1 | tail -20
```
Expected: clean build, 0 errors.

- [ ] **Step 2: Add fan_out_meta to TaskNodeView interface**

In `ui/workspace/src/TaskGraphCanvas.tsx`, find the `interface TaskNodeView` block and add fields:
```typescript
interface FanOutMeta {
  source_artifact_role: string;
  total_instances: number;
  completed_instances: number;
  producer_task_id?: string | null;
}

interface TaskNodeView {
  // ... existing fields ...
  fan_out_meta?: FanOutMeta | null;
}
```

- [ ] **Step 3: Add FanOutCardNode component**

In `TaskGraphCanvas.tsx`, after the `TaskCardNode` component definition, add:

```typescript
const STATUS_LABEL_FAN_OUT: Record<string, string> = {
  waiting_for_fan_out_source: "Ожидает данных",
  waiting_for_children: "В процессе",
  completed: "Завершено",
  failed: "Ошибка",
};

const STATUS_COLOR_FAN_OUT: Record<string, string> = {
  waiting_for_fan_out_source: "#94a3b8",
  waiting_for_children: "#3b82f6",
  completed: "#22c55e",
  failed: "#ef4444",
};

function FanOutCardNode({ data }: { data: TaskNodeView }) {
  const meta = data.fan_out_meta;
  const statusLabel = STATUS_LABEL_FAN_OUT[data.status] ?? data.status;
  const statusColor = STATUS_COLOR_FAN_OUT[data.status] ?? "#94a3b8";

  return (
    <div
      style={{
        background: "#fff",
        border: `2px dashed ${statusColor}`,
        borderRadius: 10,
        padding: "8px 12px",
        minWidth: 200,
        maxWidth: 260,
        fontSize: 12,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontWeight: 700, fontSize: 11, color: "#7c3aed" }}>⚡ fan-out</span>
        <span style={{ background: statusColor, color: "#fff", borderRadius: 4, padding: "1px 6px", fontSize: 10 }}>
          {statusLabel}
        </span>
      </div>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{data.title}</div>
      {data.status === "waiting_for_fan_out_source" && meta && (
        <div style={{ color: "#94a3b8", fontSize: 11 }}>
          Источник: {meta.source_artifact_role}
        </div>
      )}
      {data.status === "waiting_for_children" && meta && meta.total_instances > 0 && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 11, color: "#6b7280", marginBottom: 2 }}>
            {meta.completed_instances} / {meta.total_instances} завершено
          </div>
          <div style={{ height: 4, background: "#e5e7eb", borderRadius: 2 }}>
            <div
              style={{
                height: "100%",
                width: `${(meta.completed_instances / meta.total_instances) * 100}%`,
                background: "#3b82f6",
                borderRadius: 2,
                transition: "width 0.3s",
              }}
            />
          </div>
        </div>
      )}
      {data.status === "failed" && data.error_message && (
        <div style={{ color: "#ef4444", fontSize: 11, marginTop: 4 }}>{data.error_message}</div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Register FanOutCardNode in nodeTypes and adapt graph flattening**

Find the `nodeTypes` declaration (e.g., `const nodeTypes = { taskCard: TaskCardNode }`) and add:
```typescript
const nodeTypes = {
  taskCard: TaskCardNode,
  fanOutCard: FanOutCardNode,
};
```

In the `flattenTree` / node-building loop, set `type: "fanOutCard"` for fan-out nodes:
```typescript
// when building ReactFlow nodes from TaskNodeView:
type: node.template_type === "fan_out" ? "fanOutCard" : "taskCard",
```

- [ ] **Step 5: Add adaptive collapse state**

Near the top of `TaskGraphCanvas` component, add state:
```typescript
const [collapsedFanOuts, setCollapsedFanOuts] = useState<Set<string>>(new Set());
```

When building the flat node list, skip children of collapsed fan-out nodes AND fan-out nodes with > 4 children should start collapsed:
```typescript
// After computing the flat node list, before rendering,
// for each fan_out node: if childCount > 4 and not in initial render, auto-collapse
// Initial setup effect:
useEffect(() => {
  const toCollapse = new Set<string>();
  flatNodes.forEach((n) => {
    if (n.data.template_type === "fan_out") {
      const childCount = n.data.fan_out_meta?.total_instances ?? 0;
      if (childCount > 4) toCollapse.add(n.id);
    }
  });
  setCollapsedFanOuts(toCollapse);
}, []); // run once on mount
```

In the node flattening logic, skip children of collapsed fan-out nodes:
```typescript
function shouldIncludeNode(node: TaskNodeView, collapsedSet: Set<string>): boolean {
  if (!node.parent_task_id) return true;
  if (collapsedSet.has(node.parent_task_id)) return false;
  return true;
}
```

On `FanOutCardNode`, add a toggle button (pass `onToggle` via `data`):
```typescript
// In FanOutCardNode, after the progress bar:
{data.onToggleFanOut && meta && meta.total_instances > 4 && (
  <button
    onClick={() => data.onToggleFanOut!(data.task_id)}
    style={{ marginTop: 6, fontSize: 10, cursor: "pointer", background: "none", border: "1px solid #d1d5db", borderRadius: 4, padding: "2px 8px" }}
  >
    {data.isCollapsed ? `Показать все ${meta.total_instances}` : "Свернуть"}
  </button>
)}
```

- [ ] **Step 6: Add dashed producer edge**

When building edges in `TaskGraphCanvas`, after the standard parent→child edges, add synthetic dashed edges for fan-out→producer:
```typescript
// Find producer_task_id from fan_out_meta and draw a dashed edge
flatNodes.forEach((n) => {
  if (n.data.template_type === "fan_out" && n.data.fan_out_meta?.producer_task_id) {
    edges.push({
      id: `producer-${n.data.fan_out_meta.producer_task_id}-${n.id}`,
      source: n.data.fan_out_meta.producer_task_id,
      target: n.id,
      style: { stroke: "#8B5CF6", strokeDasharray: "6 3", strokeWidth: 2 },
      animated: false,
      type: "default",
    });
  }
});
```

- [ ] **Step 7: Verify TypeScript compiles clean**

```bash
cd ui/workspace && npm run build 2>&1 | tail -30
```
Expected: 0 TypeScript errors, build succeeds.

- [ ] **Step 8: Commit**

```bash
git add ui/workspace/src/TaskGraphCanvas.tsx
git commit -m "feat(ui): add FanOutCardNode, adaptive collapse, dashed producer edge to TaskGraphCanvas"
```

---

## Task 6: Registry validation test update

**Files:**
- Modify: `tests/test_foundation.py`

- [ ] **Step 1: Update registry count assertions**

In `test_foundation.py`, in `test_registry_validation_passes_for_task_graph_corpus`, the test checks `len(snapshot.templates) >= 21`. This remains valid since we only added templates in test fixtures, not in the main `templates/` directory. No count change needed unless you add a real fan-out template to `templates/`.

Add a new test for fan-out validator rules:
```python
def test_registry_validation_rejects_fan_out_template_without_fan_out_spec(tmp_path: Path) -> None:
    import yaml
    registry_root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", registry_root)
    bad_dir = registry_root / "tasks" / "bad_fan_out"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "bad.yaml").write_text(yaml.dump({
        "id": "bad.fan_out_no_spec",
        "version": "1.0.0",
        "kind": "task_template",
        "type": "fan_out",
        "title": "Bad fan-out",
        # missing fan_out_spec and children_template_ref
        "children": [],
        "slots": [],
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }, allow_unicode=True), encoding="utf-8")

    registry_service, _, _, _ = build_services(registry_root)
    _, report = registry_service.validate()

    assert not report.is_valid
    assert any("fan_out_spec" in issue.message for issue in report.errors)
```

- [ ] **Step 2: Run the new test**

```bash
python -m pytest tests/test_foundation.py::test_registry_validation_rejects_fan_out_template_without_fan_out_spec -q
```
Expected: `1 passed`

- [ ] **Step 3: Run full suite one final time**

```bash
python -m pytest -q
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_foundation.py
git commit -m "test(registry): add fan_out template validator rule test"
```

---

## Self-Review Notes

- **Spec coverage check:**
  - ✅ `fan_out` template_type + `FanOutSpec` in registry (Task 2)
  - ✅ `waiting_for_fan_out_source` status + `expand_fan_out`/`reset_fan_out` commands (Task 1)
  - ✅ Lazy fan-out expansion triggered by completed artifact (Task 3)
  - ✅ Idempotency via `stable_key` (Task 3 — `_expand_fan_outs` checks `find_task_by_stable_key`)
  - ✅ Empty array → immediate `expand_fan_out` (Task 3 — loop over empty array still calls `transition_task`)
  - ✅ Fan-out completes when all children done (Task 3 — `_refresh_composite_completion_from_tasks`)
  - ✅ `FanOutMeta` in `TaskNodeView` (Task 4)
  - ✅ `FanOutCardNode` (Task 5)
  - ✅ Adaptive collapse ≤4 / >4 (Task 5)
  - ✅ Dashed producer edge (Task 5)
  - ✅ Registry validator rules (Task 2 + Task 6)
  - ⚠️ `reset_fan_out` (producer retry) — state machine is in Task 1, but no planning-level trigger for it. This is intentional: the reset is applied manually or by a future "producer retry" hook. Noted as follow-up.

- **Type consistency:** `FanOutMeta` defined in `workspace_views.py`, imported in `workspace_query_service.py` and test. `fan_out_meta` field name consistent across all files.

- **No placeholders:** all code blocks are complete and runnable.
