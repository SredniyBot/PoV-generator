"""Идея А (v3.10): самоотчётные решения из ответа генерации.

DecisionExtractionService больше не делает отдельный LLM-вызов — он персистит
«сырые» решения, которые модель вернула в поле `decisions` ответа генерации,
с дедупликацией относительно реестра.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.decision_extraction_service import (
    DecisionExtractionService,
    decisions_schema,
)
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


def _raw(title: str) -> dict:
    return {
        "title": title,
        "description": "Краткое описание выбора",
        "category": "tech_stack",
        "alternatives": [
            {"option_id": "a", "label": "Вариант A", "description": "да", "confidence": 0.7},
            {"option_id": "b", "label": "Вариант B", "description": "дб", "confidence": 0.4},
        ],
        "chosen_in_artifact_option_id": "a",
        "rationale": "так лучше ложится на требования",
        "level": "architecture",
        "confidence": 0.6,
    }


def test_persist_self_reported_saves_as_accepted_default(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    svc = DecisionExtractionService(runtime)

    saved = svc.persist_self_reported(
        ws,
        project_id="p1",
        artifact_id="art-1",
        task_id="t-1",
        raw_decisions=[_raw("Выбор СУБД"), _raw("Выбор OCR-движка")],
    )
    assert len(saved) == 2
    for d in saved:
        assert d.status == "accepted_default"
        assert d.source == "emergent"
        assert d.user_action == "not_shown"
        assert d.affected_artifact_ids == ("art-1",)


def test_persist_self_reported_dedups_against_registry(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    svc = DecisionExtractionService(runtime)

    svc.persist_self_reported(
        ws, project_id="p1", artifact_id="art-1", task_id="t-1",
        raw_decisions=[_raw("Выбор СУБД"), _raw("Выбор OCR-движка")],
    )
    # Повторный заголовок (в т.ч. с другим регистром/пробелами) не дублируется.
    saved2 = svc.persist_self_reported(
        ws, project_id="p1", artifact_id="art-2", task_id="t-2",
        raw_decisions=[_raw("выбор  субд"), _raw("Выбор очереди задач")],
    )
    assert {d.title for d in saved2} == {"Выбор очереди задач"}
    assert len(runtime.list_decisions(ws, project_id="p1")) == 3


def test_persist_self_reported_empty_is_noop(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    svc = DecisionExtractionService(runtime)
    assert svc.persist_self_reported(
        ws, project_id="p1", artifact_id="a", task_id="t", raw_decisions=[]
    ) == ()


def test_persist_self_reported_parses_enriched_fields(tmp_path: Path) -> None:
    """v3.11: level_rationale/evidence/confidence из ответа генерации
    попадают в Decision; низкая confidence оживляет is_low_confidence."""
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    svc = DecisionExtractionService(runtime)

    raw = _raw("Выбор СУБД")
    raw["level_rationale"] = "Долгоиграющий технический выбор — архитектурный уровень."
    raw["evidence"] = "Артефакт закладывает реляционную модель данных."
    raw["confidence"] = 0.25

    saved = svc.persist_self_reported(
        ws, project_id="p1", artifact_id="art-1", task_id="t-1", raw_decisions=[raw]
    )
    d = saved[0]
    assert d.level_rationale.startswith("Долгоиграющий технический выбор")
    assert "реляционную модель" in d.evidence
    assert d.confidence == 0.25
    assert d.is_low_confidence is True


def test_persist_self_reported_attaches_reference_provenance(tmp_path: Path) -> None:
    """Эмерджентное решение несёт reference-снимок: call-level поля из
    provenance_base + raw_item. Тяжёлый prompt НЕ дублируется (ссылка на
    execution_run_id, трейсы гидрируются на чтении)."""
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    svc = DecisionExtractionService(runtime)

    base = {
        "execution_run_id": "run-9",
        "provider": "claude_sdk",
        "model": "opus",
        "token_usage": {"total_tokens": 100},
    }
    saved = svc.persist_self_reported(
        ws, project_id="p1", artifact_id="art-1", task_id="t-1",
        raw_decisions=[_raw("Выбор СУБД")], provenance_base=base,
    )
    prov = saved[0].provenance
    assert prov["source_kind"] == "emergent"
    assert prov["execution_run_id"] == "run-9"
    assert prov["provider"] == "claude_sdk"
    assert prov["raw_item"]["title"] == "Выбор СУБД"
    # reference-снимок не хранит сырой prompt — он за execution_run_id.
    assert "prompt" not in prov


def test_decisions_schema_is_optional_array_of_decisions() -> None:
    schema = decisions_schema()
    assert schema["type"] == "array"
    item = schema["items"]
    assert "title" in item["properties"]
    # Единая облегчённая схема: рекомендация по label (вместо машинного
    # chosen_in_artifact_option_id), alternatives = {label, description}.
    assert "recommended" in item["properties"]
    assert set(item["properties"]["alternatives"]["items"]["properties"]) == {"label", "description"}
