# tests/test_fan_out_tasks.py
from __future__ import annotations

import pytest

from pov_generator.common.errors import ConflictError
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.tasks import (
    TaskRecord,
    apply_task_command,
    initial_task_status,
)


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
