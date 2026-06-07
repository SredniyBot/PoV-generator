"""Ф6 — живой статус harness-рантайма («машинное отделение»).

Слоты класса конкуррентности + очередь, накопленный расход и лимиты прогона
сводятся в один снимок (`runtime_status`), делегируемый из `ExecutionService` и
отдаваемый эндпоинтом `/api/harness/runtime`. Без Docker (stub-провайдер).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.harness_execution_service import (
    HarnessExecutionService,
    HarnessRuntimeStatus,
)
from pov_generator.infrastructure.harness import HarnessSlotPool
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_status_reports_provider_slots_budget() -> None:
    service = HarnessExecutionService(slots=HarnessSlotPool(3))
    status = service.runtime_status()

    assert isinstance(status, HarnessRuntimeStatus)
    assert status.provider_name == "stub"
    assert (status.slots.capacity, status.slots.in_use, status.slots.waiting) == (3, 0, 0)
    assert status.slots.available == 3
    # До прогонов расход нулевой, кумулятивный потолок не превышен.
    assert status.budget.runs == 0
    assert status.budget.total_tokens == 0
    assert status.budget_exceeded is None
    assert status.run_limits is not None


def test_runtime_status_reflects_runs() -> None:
    service = HarnessExecutionService()
    service.produce_artifact_payload(
        artifact_role="demo_output",
        system_prompt="SYS",
        user_prompt="USR",
    )
    status = service.runtime_status()
    # Слот освобождён после прогона (контекст-менеджер), расход учтён.
    assert status.slots.in_use == 0
    assert status.budget.runs == 1


def test_api_harness_runtime_endpoint(tmp_path: Path) -> None:
    app = create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime")
    client = TestClient(app)

    resp = client.get("/api/harness/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_name"] == "stub"
    assert body["slots"]["capacity"] >= 1
    assert body["slots"]["in_use"] == 0
    assert body["budget"]["runs"] == 0
    assert "run_limits" in body
