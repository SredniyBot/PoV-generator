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
