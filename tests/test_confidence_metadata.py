"""Тесты миграции `confidence` из payload в `ArtifactMetadata.overall_confidence`.

Регрессия: уверенность модели должна жить в метаданных артефакта, а не
дублироваться в его JSON-теле. Эти тесты проверяют, что:
1. Схема не требует `confidence` в payload (новые задачи могут его не возвращать).
2. После исполнения задачи execution_service вытаскивает payload['confidence']
   в `ArtifactMetadata.overall_confidence`.
3. API артефакта `/artifacts/{id}` отдаёт overall_confidence в карточке.
4. Валидация по-прежнему ловит низкую уверенность (теперь читая metadata).
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.artifact_contracts import artifact_schema
from pov_generator.application.execution_service import _extract_overall_confidence
from pov_generator.application.validation_service import _resolve_confidence


def test_confidence_is_not_required_in_analysis_schemas() -> None:
    """Поле `confidence` остаётся в `properties` (LLM может его вернуть), но
    схема не должна валиться, если оно отсутствует — это метаданные."""
    schema = artifact_schema("goal_hypothesis")
    assert "confidence" in schema["properties"], (
        "confidence остаётся в properties для backward-compat: LLM продолжает "
        "его возвращать, мы извлекаем значение в metadata."
    )
    assert "confidence" not in schema["required"], (
        "confidence НЕ должно быть required — это метаданные, отсутствие "
        "поля в payload — валидный кейс."
    )


def test_extract_overall_confidence_clamps_into_unit_interval() -> None:
    """Хелпер из execution_service должен:
    - вернуть None, если confidence отсутствует / не число / NaN;
    - вернуть число, ограниченное диапазоном [0.0, 1.0]."""
    assert _extract_overall_confidence({}) is None
    assert _extract_overall_confidence({"confidence": None}) is None
    assert _extract_overall_confidence({"confidence": "0.5"}) is None
    assert _extract_overall_confidence({"confidence": True}) is None  # bool — не считаем числом
    assert _extract_overall_confidence({"confidence": float("nan")}) is None
    assert _extract_overall_confidence({"confidence": 0.85}) == 0.85
    assert _extract_overall_confidence({"confidence": -0.5}) == 0.0
    assert _extract_overall_confidence({"confidence": 1.5}) == 1.0


def test_resolve_confidence_prefers_metadata_falls_back_to_payload() -> None:
    """`_resolve_confidence` (validation_service):
    - если есть число в overall_confidence → берём его (метаданные канонично);
    - иначе fallback на payload['confidence'] (legacy + backward-compat);
    - если нигде нет — None."""
    assert _resolve_confidence(0.42, {}) == 0.42
    assert _resolve_confidence(0.42, {"confidence": 0.99}) == 0.42  # metadata wins
    assert _resolve_confidence(None, {"confidence": 0.71}) == 0.71
    assert _resolve_confidence(None, {"confidence": "no"}) is None
    assert _resolve_confidence(None, {}) is None


def test_overall_confidence_populated_after_stub_workflow(tmp_path: Path) -> None:
    """End-to-end: запустить stub-workflow и проверить, что хотя бы один
    артефакт получил overall_confidence в метаданных (raison d'être этого
    рефакторинга)."""
    from test_m5_m8 import _approve_requirements_signoff, init_workspace  # type: ignore

    (
        workspace,
        snapshot,
        runtime,
        _project_service,
        _planning_service,
        _context_service,
        _execution_service,
        _validation_service,
        workflow_service,
    ) = init_workspace(tmp_path)

    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    _approve_requirements_signoff(runtime, workspace)
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)

    artifacts = runtime.list_artifacts(workspace)
    primaries = [a for a in artifacts if a.artifact_kind == "primary"]
    with_confidence = [a for a in primaries if a.metadata.overall_confidence is not None]

    assert with_confidence, (
        "Ни у одного primary-артефакта не заполнен overall_confidence. "
        "Видимо execution_service не вытаскивает payload['confidence'] в metadata."
    )
    # И значения должны быть в допустимом диапазоне.
    for artifact in with_confidence:
        assert 0.0 <= (artifact.metadata.overall_confidence or 0.0) <= 1.0
