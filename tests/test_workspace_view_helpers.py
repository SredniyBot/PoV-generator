"""Unit-тесты на эвристики L6 backend (P3 v2 skeleton, P5/P7/P8).

Здесь — только чистые helpers, не требующие настоящего workspace.
Интеграционные пути проверяются в существующих тестах workflow.
"""

from __future__ import annotations

from pov_generator.application.workspace_query_service import WorkspaceQueryService
from pov_generator.domain.workspace_views import ArtifactSectionView


def test_humanize_key_snake_case() -> None:
    assert WorkspaceQueryService._humanize_key("jtbd_analysis") == "Jtbd analysis"
    assert WorkspaceQueryService._humanize_key("functional-requirements") == "Functional requirements"
    assert WorkspaceQueryService._humanize_key("risks") == "Risks"


def test_humanize_key_edge_cases() -> None:
    assert WorkspaceQueryService._humanize_key("") == ""
    assert WorkspaceQueryService._humanize_key("_") == "_"


def test_is_section_empty_handles_typical_inputs() -> None:
    assert WorkspaceQueryService._is_section_empty(None) is True
    assert WorkspaceQueryService._is_section_empty("") is True
    assert WorkspaceQueryService._is_section_empty("   ") is True
    assert WorkspaceQueryService._is_section_empty("TBD") is True
    assert WorkspaceQueryService._is_section_empty("—") is True
    assert WorkspaceQueryService._is_section_empty([]) is True
    assert WorkspaceQueryService._is_section_empty({}) is True
    assert WorkspaceQueryService._is_section_empty("Реальное содержимое") is False
    assert WorkspaceQueryService._is_section_empty(["item"]) is False
    assert WorkspaceQueryService._is_section_empty({"a": 1}) is False


def test_section_summary_truncates_strings() -> None:
    long = "x" * 500
    summary = WorkspaceQueryService._section_summary(long)
    assert summary is not None and len(summary) == 200


def test_section_summary_describes_collections() -> None:
    assert WorkspaceQueryService._section_summary(["a", "b", "c"]) == "3 элементов"
    assert WorkspaceQueryService._section_summary({"a": 1, "b": 2}) == "2 полей"
    assert WorkspaceQueryService._section_summary(None) is None
    assert WorkspaceQueryService._section_summary([]) is None


def test_format_version_label_with_and_without_date() -> None:
    assert WorkspaceQueryService._format_version_label(0, None) == "v1"
    assert WorkspaceQueryService._format_version_label(2, "2026-05-11T13:42:00Z") == "v3 · 2026-05-11"


def test_severity_mapping() -> None:
    assert WorkspaceQueryService._severity_from_priority("critical") == "high"
    assert WorkspaceQueryService._severity_from_priority("high") == "high"
    assert WorkspaceQueryService._severity_from_priority("medium") == "medium"
    assert WorkspaceQueryService._severity_from_priority("low") == "low"
    # unknown → fallback to medium
    assert WorkspaceQueryService._severity_from_priority("weird") == "medium"
    assert WorkspaceQueryService._severity_from_priority(None) == "medium"


# --- _extract_artifact_sections ----------------------------------------------


def _extract(svc: WorkspaceQueryService, data: object) -> tuple[ArtifactSectionView, ...]:
    return svc._extract_artifact_sections(data, {})


def test_extract_sections_none_returns_pending_placeholder() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    sections = _extract(svc, None)
    assert len(sections) == 1
    assert sections[0].section_id == "content"
    assert sections[0].status == "pending"


def test_extract_sections_from_top_level_dict() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    data = {
        "context": "Описание контекста проекта",
        "jtbd_analysis": ["job1", "job2"],
        "functional_requirements": "",  # пустое → pending
        "_meta": {"created": "2026"},  # служебное → отбрасывается
        "metadata": {"id": "x"},  # служебное → отбрасывается
    }
    sections = _extract(svc, data)
    ids = [s.section_id for s in sections]
    assert "context" in ids
    assert "jtbd_analysis" in ids
    assert "functional_requirements" in ids
    assert "_meta" not in ids
    assert "metadata" not in ids
    statuses = {s.section_id: s.status for s in sections}
    assert statuses["context"] == "done"
    assert statuses["jtbd_analysis"] == "done"
    assert statuses["functional_requirements"] == "pending"


def test_extract_sections_explicit_sections_field() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    data = {
        "sections": [
            {"id": "intro", "title": "Введение", "content": "Текст"},
            {"id": "scope", "title": "Скоуп", "content": ""},
            {"id": "extras", "title": "Доп", "content": ["a", "b"]},
        ]
    }
    sections = _extract(svc, data)
    assert [s.section_id for s in sections] == ["intro", "scope", "extras"]
    assert sections[0].status == "done"
    assert sections[1].status == "pending"
    assert sections[2].status == "done"
    assert sections[0].title == "Введение"


def test_extract_sections_from_list() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    data = ["a", "b", "c"]
    sections = _extract(svc, data)
    assert [s.section_id for s in sections] == ["item_1", "item_2", "item_3"]
    assert sections[0].title == "Пункт 1"


def test_extract_sections_primitive() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    sections = _extract(svc, "просто текст артефакта")
    assert len(sections) == 1
    assert sections[0].status == "done"
    assert sections[0].summary == "просто текст артефакта"


def test_extract_sections_pins_flip_status_to_needs_review() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    data = {"context": "Готовый текст", "scope": "тоже готов"}
    sections = svc._extract_artifact_sections(data, {"context": 2})
    by_id = {s.section_id: s for s in sections}
    assert by_id["context"].status == "needs_review"
    assert by_id["context"].pin_count == 2
    assert by_id["context"].has_pins is True
    assert by_id["scope"].status == "done"
    assert by_id["scope"].pin_count == 0


def test_extract_sections_empty_dict_yields_default_placeholder() -> None:
    svc = WorkspaceQueryService.__new__(WorkspaceQueryService)
    sections = _extract(svc, {})
    assert len(sections) == 1
    assert sections[0].section_id == "content"
