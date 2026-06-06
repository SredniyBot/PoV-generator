"""Общий механизм диаграмм (Ф1 переработки архитектуры).

Любой артефакт может нести блок ``diagrams`` — список диаграмм с семантическим
типом, подписью и структурным ``spec``. Форма (flowchart/sequence) выводится из
spec. Тесты фиксируют:

1. ``_build_diagram`` — выбор формы (явный kind + вывод по форме);
2. ``_render_diagrams`` — подпись по умолчанию / перекрытие / устойчивость к мусору;
3. поведение-сохраняюще: рендеры архитектурных артефактов без ``diagrams`` не
   меняются, а с ``diagrams`` — дописывают блоки.
"""

from __future__ import annotations

from pov_generator.application.artifact_contracts import (
    _build_diagram,
    _diagram_block_schema,
    _diagrams_array_schema,
    _render_diagrams,
    _render_mermaid_block,
    render_markdown,
)

_FLOW_SPEC = {
    "nodes": [
        {"id": "A", "label": "Первый"},
        {"id": "B", "label": "Второй"},
    ],
    "edges": [{"from": "A", "to": "B", "label": "идёт"}],
}
_SEQ_SPEC = {
    "participants": [{"id": "U", "label": "User"}, {"id": "S", "label": "System"}],
    "messages": [{"from": "U", "to": "S", "label": "запрос"}],
}


# --- 1. _build_diagram ------------------------------------------------------


def test_build_diagram_infers_flowchart_from_shape() -> None:
    """Spec с nodes/edges и без kind → flowchart (а не sequence по умолчанию)."""
    mmd = _build_diagram(_FLOW_SPEC)
    assert mmd.startswith("flowchart")
    assert "A" in mmd and "B" in mmd


def test_build_diagram_infers_sequence_from_shape() -> None:
    """Spec с participants/messages → sequenceDiagram."""
    mmd = _build_diagram(_SEQ_SPEC)
    assert mmd.startswith("sequenceDiagram")


def test_build_diagram_explicit_kind_wins() -> None:
    mmd_flow = _build_diagram({"kind": "flowchart", **_FLOW_SPEC})
    assert mmd_flow.startswith("flowchart")
    mmd_seq = _build_diagram({"kind": "sequence", **_SEQ_SPEC})
    assert mmd_seq.startswith("sequenceDiagram")


def test_build_diagram_none_and_garbage_are_empty() -> None:
    assert _build_diagram(None) == ""
    assert _build_diagram("not a dict") == ""  # type: ignore[arg-type]
    assert _build_diagram({}) == "flowchart LR"  # пустой flowchart — валидно


# --- 2. _render_diagrams ----------------------------------------------------


def test_render_diagrams_default_caption_by_type() -> None:
    out = "\n".join(_render_diagrams([{"type": "deployment", "spec": _FLOW_SPEC}]))
    assert "**Диаграмма развёртывания:**" in out
    assert "```mermaid" in out


def test_render_diagrams_caption_overrides_type() -> None:
    out = "\n".join(
        _render_diagrams([{"type": "data", "caption": "Моя картинка", "spec": _FLOW_SPEC}])
    )
    assert "**Моя картинка:**" in out


def test_render_diagrams_skips_broken_and_empty() -> None:
    assert _render_diagrams(None) == []
    assert _render_diagrams("nope") == []  # type: ignore[arg-type]
    assert _render_diagrams([]) == []
    # битый элемент и элемент без рисуемого spec пропускаются
    assert _render_diagrams(["x", {"type": "data"}, {"type": "data", "spec": 5}]) == []


def test_render_diagrams_renders_multiple() -> None:
    out = "\n".join(
        _render_diagrams(
            [
                {"type": "components", "spec": _FLOW_SPEC},
                {"type": "sequence", "spec": _SEQ_SPEC},
            ]
        )
    )
    assert out.count("```mermaid") == 2
    assert "**Диаграмма компонентов:**" in out
    assert "**Диаграмма последовательности:**" in out


def test_render_mermaid_block_empty_is_empty_list() -> None:
    assert _render_mermaid_block("", "Заголовок") == []
    assert _render_mermaid_block("flowchart LR", "Заголовок")[0] == "\n**Заголовок:**"


# --- 3. Поведение-сохраняюще + аддитивность в реальных рендерах -------------


def _design_payload(with_diagrams: bool) -> dict:
    payload = {
        "title": "Система X",
        "executive_summary": "Кратко.",
        "system_context": {
            "system_purpose": "Назначение.",
            "actors": [{"name": "Менеджер", "kind": "user"}],
            "external_systems": [{"name": "CRM", "role": "источник"}],
            "context_diagram": _FLOW_SPEC,
        },
    }
    if with_diagrams:
        payload["diagrams"] = [{"type": "deployment", "spec": _FLOW_SPEC}]
    return payload


def test_design_document_without_diagrams_has_no_extra_block() -> None:
    md = render_markdown("design_document", _design_payload(with_diagrams=False))
    # фиксированный слот контекстной диаграммы остаётся
    assert "**Контекстная диаграмма:**" in md
    # общий блок не добавляет диаграмму развёртывания
    assert "**Диаграмма развёртывания:**" not in md


def test_design_document_with_diagrams_appends_block() -> None:
    md = render_markdown("design_document", _design_payload(with_diagrams=True))
    assert "**Контекстная диаграмма:**" in md
    assert "**Диаграмма развёртывания:**" in md


# --- 4. Схемы ---------------------------------------------------------------


def test_diagram_block_schema_shape() -> None:
    schema = _diagram_block_schema()
    assert schema["required"] == ["type", "spec"]
    assert set(schema["properties"]) == {"type", "caption", "of", "spec"}
    # все семантические типы перечислены закрытым enum
    assert "deployment" in schema["properties"]["type"]["enum"]
    assert schema["additionalProperties"] is False


def test_diagrams_array_schema_wraps_block() -> None:
    schema = _diagrams_array_schema()
    assert schema["type"] == "array"
    assert schema["items"]["required"] == ["type", "spec"]
