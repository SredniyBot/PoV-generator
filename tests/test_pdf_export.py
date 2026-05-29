"""Тесты для PDF-экспорта артефактов.

Покрывают:
- pdf_export.render_artifact_pdf — конвертация markdown → PDF (bytes).
- /api/projects/{id}/artifacts/{aid}/download.pdf endpoint.

Тестируем структуру/инварианты, а не точное содержание — внутреннее
устройство PDF (offsets, font tables) зависит от reportlab версии.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_m9_api import init_project, run_stub_workflow  # type: ignore

from pov_generator.application import mermaid_render, pdf_export
from pov_generator.application.pdf_export import (
    _replace_mermaid_blocks_with_images,
    render_artifact_pdf,
)
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_artifact_pdf_produces_valid_pdf_bytes() -> None:
    """Минимальный markdown превращается в валидный PDF (начинается с %PDF-)."""
    md = (
        "# Тестовый артефакт\n\n"
        "Это **жирный** текст и *курсив* для проверки кириллицы.\n\n"
        "## Список\n\n"
        "- Пункт раз\n"
        "- Пункт два\n\n"
        "## Таблица\n\n"
        "| Колонка | Значение |\n"
        "|---|---|\n"
        "| Альфа | 42 |\n"
        "| Бета | 7 |\n"
    )

    pdf_bytes = render_artifact_pdf(markdown_content=md, title="Тест")

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500, "PDF подозрительно маленький"
    # PDF файлы всегда начинаются с этой сигнатуры.
    assert pdf_bytes.startswith(b"%PDF-"), "Не похоже на PDF-байты"
    # И заканчиваются маркером EOF.
    assert b"%%EOF" in pdf_bytes[-32:]


def test_render_artifact_pdf_handles_empty_title() -> None:
    """title=None не падает — используется дефолт."""
    pdf_bytes = render_artifact_pdf(markdown_content="# Привет", title=None)
    assert pdf_bytes.startswith(b"%PDF-")


def test_wide_table_produces_landscape_page_in_pdf() -> None:
    """E2E: широкая 7-колоночная таблица реально приводит к landscape-странице.

    Проверяем MediaBox в финальных PDF-байтах. A4 portrait = 595×842 pt,
    landscape = 842×595. Если landscape не сработал, обе страницы будут
    одинаковой ориентации.
    """
    import re

    md = (
        "# Реестр рисков\n\n"
        "| ID | Описание риска | Вероятность | Влияние | Митигация | Владелец | Срок |\n"
        "|---|---|---|---|---|---|---|\n"
        "| R-001 | Поставщик данных задерживает интеграцию из-за реорганизации в их департаменте | "
        "Средняя | Высокое | Закрепить SLA в контракте; держать буфер 2 недели в плане | "
        "Архитектор интеграций | 2026-06-01 |\n"
        "| R-002 | Регуляторное требование по локализации данных меняется в течение проекта | "
        "Низкая | Критическое | Юридическая проверка раз в квартал; гибкая архитектура | "
        "DPO | непрерывно |\n\n"
        "## Следующий раздел\n\n"
        "После таблицы — обычный портретный текст.\n"
    )

    pdf_bytes = render_artifact_pdf(markdown_content=md, title="Risk Register")
    data_str = pdf_bytes.decode("latin-1", errors="replace")
    mediaboxes = re.findall(r'/MediaBox\s*\[([^\]]+)\]', data_str)

    orientations = []
    for mb in mediaboxes:
        parts = mb.strip().split()
        width, height = float(parts[2]), float(parts[3])
        orientations.append("landscape" if width > height else "portrait")

    assert "landscape" in orientations, (
        f"Широкая таблица не привела к landscape-странице. Ориентации страниц: {orientations}"
    )
    assert "portrait" in orientations, (
        f"После landscape-таблицы следующий раздел должен вернуться в portrait. "
        f"Ориентации страниц: {orientations}"
    )


def test_narrow_table_keeps_portrait_only() -> None:
    """Регрессия: маленькая таблица (2 колонки) не должна триггерить landscape."""
    import re

    md = (
        "# Простая таблица\n\n"
        "| Поле | Значение |\n"
        "|---|---|\n"
        "| Имя | Альфа |\n"
        "| Тип | Запрос |\n"
    )
    pdf_bytes = render_artifact_pdf(markdown_content=md, title="Narrow")
    data_str = pdf_bytes.decode("latin-1", errors="replace")
    mediaboxes = re.findall(r'/MediaBox\s*\[([^\]]+)\]', data_str)

    for mb in mediaboxes:
        parts = mb.strip().split()
        width, height = float(parts[2]), float(parts[3])
        assert width < height, (
            f"Узкая таблица ушла в landscape ({width}x{height}) — ложное срабатывание порога"
        )


def test_render_artifact_pdf_embeds_unicode_font_for_cyrillic() -> None:
    """Регрессия: кириллица не должна рендериться через core-PDF Helvetica
    (она не имеет Cyrillic-глифов → чёрные квадраты в PDF-вьюверах).

    Проверяем, что в выводном PDF есть встроенный subset-шрифт
    (имя вида ``ABCDEF+FontName``), а не только core-шрифты.
    """
    import re

    pdf_bytes = render_artifact_pdf(
        markdown_content="# Заголовок\n\nКириллический параграф для проверки.",
        title="Test",
    )
    data = pdf_bytes.decode("latin-1", errors="replace")
    base_fonts = set(re.findall(r"/BaseFont\s*/([\w+-]+)", data))
    # Встроенный subset reportlab именует как `<6 latin caps>+FontName`.
    subset_fonts = [name for name in base_fonts if re.match(r"^[A-Z]{6}\+", name)]
    assert subset_fonts, (
        f"В PDF не встроен ни один Unicode-subset (есть только core-fonts: {base_fonts}). "
        "Кириллица будет отображаться как чёрные квадраты."
    )


def test_download_pdf_endpoint_returns_pdf_for_real_artifact(tmp_path: Path) -> None:
    """End-to-end: гоняем stub-workflow, скачиваем итоговый ТЗ как PDF."""
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case_pdf"
    project_id = init_project(
        workspace,
        "Нужно подготовить ТЗ для сервиса, который структурирует бизнес-запросы.",
    )
    run_stub_workflow(workspace)

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    spec_id = next(item["artifact_id"] for item in artifacts if item["artifact_role"] == "requirements_spec")

    response = client.get(f"/api/projects/{project_id}/artifacts/{spec_id}/download.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".pdf" in disposition
    body = response.content
    assert body.startswith(b"%PDF-")
    assert len(body) > 1000


def test_download_pdf_endpoint_returns_404_when_no_markdown(tmp_path: Path) -> None:
    """Если у артефакта нет markdown (например, кастомный артефакт без
    рендерера), endpoint возвращает 404 с понятным сообщением."""
    from unittest.mock import patch

    from pov_generator.domain.workspace_views import ArtifactDetailView

    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case_no_md"
    project_id = init_project(
        workspace,
        "Минимальный запрос для проверки PDF-эндпойнта.",
    )
    run_stub_workflow(workspace)

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    target_id = artifacts[0]["artifact_id"]

    fake_view = ArtifactDetailView(
        artifact_id=target_id,
        artifact_role="some_role",
        title="No-markdown artifact",
        description="",
        created_at="2026-01-01T00:00:00+00:00",
        created_by_task_id=None,
        template_ref=None,
        json_content="{}",
        markdown_content=None,
    )

    with patch.object(
        app.state.query_service,
        "artifact_detail",
        return_value=fake_view,
    ):
        response = client.get(f"/api/projects/{project_id}/artifacts/{target_id}/download.pdf")

    assert response.status_code == 404
    assert "markdown" in response.json()["detail"].lower()


# --- Mermaid preprocessing (PDF) ------------------------------------------


@pytest.fixture(autouse=False)
def _reset_mermaid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    mermaid_render.clear_cache()
    monkeypatch.delenv("POV_MERMAID_DISABLED", raising=False)


def test_replace_mermaid_blocks_leaves_markdown_unchanged_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """С POV_MERMAID_DISABLED preprocessing никого не подменяет."""
    mermaid_render.clear_cache()
    monkeypatch.setenv("POV_MERMAID_DISABLED", "1")
    md = "# Title\n\n```mermaid\nflowchart LR\nA --> B\n```\n\nДалее текст."
    assert _replace_mermaid_blocks_with_images(md) == md


def test_replace_mermaid_blocks_inserts_data_uri_image_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При успешном рендере ```mermaid``` блок становится <img> с data-URI."""
    mermaid_render.clear_cache()
    monkeypatch.setattr(
        pdf_export,
        "render_mermaid_to_png",
        lambda _src: b"\x89PNG\r\n\x1a\nMINIMAL",
    )
    md = "Введение.\n\n```mermaid\nflowchart LR\nA --> B\n```\n\nЗаключение."
    out = _replace_mermaid_blocks_with_images(md)
    assert "```mermaid" not in out
    assert '<img src="data:image/png;base64,' in out
    assert 'class="mermaid-pdf"' in out
    # Исходный markdown вокруг блока сохраняется.
    assert "Введение." in out
    assert "Заключение." in out


def test_replace_mermaid_blocks_falls_back_on_render_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если render_mermaid_to_png возвращает None — оставляем code-block."""
    mermaid_render.clear_cache()
    monkeypatch.setattr(pdf_export, "render_mermaid_to_png", lambda _src: None)
    md = "```mermaid\nflowchart LR\nA --> B\n```"
    out = _replace_mermaid_blocks_with_images(md)
    assert out == md


def test_replace_mermaid_blocks_handles_multiple_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mermaid_render.clear_cache()
    counter = {"i": 0}

    def fake_render(_src: str) -> bytes:
        counter["i"] += 1
        return f"PNG-{counter['i']}".encode()

    monkeypatch.setattr(pdf_export, "render_mermaid_to_png", fake_render)
    md = (
        "```mermaid\nflowchart LR\nA --> B\n```\n\n"
        "Между.\n\n"
        "```mermaid\nsequenceDiagram\nA->>B: ping\n```\n"
    )
    out = _replace_mermaid_blocks_with_images(md)
    assert out.count('<img src="data:image/png;base64,') == 2
    assert counter["i"] == 2


def test_render_artifact_pdf_embeds_mermaid_image_when_render_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end PDF: при успешном рендере PNG попадает в финальные байты."""
    from io import BytesIO

    from PIL import Image

    mermaid_render.clear_cache()
    buf = BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
    one_pixel_png = buf.getvalue()

    monkeypatch.setattr(pdf_export, "render_mermaid_to_png", lambda _src: one_pixel_png)
    md = "# Doc\n\n```mermaid\nflowchart LR\nA --> B\n```\n"

    pdf_bytes = render_artifact_pdf(markdown_content=md, title="Mermaid in PDF")
    assert pdf_bytes.startswith(b"%PDF-")
    # В сыром PDF должен присутствовать встроенный image-object.
    assert b"/Image" in pdf_bytes or b"/XObject" in pdf_bytes


def test_render_artifact_pdf_keeps_codeblock_when_render_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """С POV_MERMAID_DISABLED PDF собирается без image-object'а (текущее поведение)."""
    mermaid_render.clear_cache()
    monkeypatch.setenv("POV_MERMAID_DISABLED", "1")
    md = "# Doc\n\n```mermaid\nflowchart LR\nA --> B\n```\n"
    pdf_bytes = render_artifact_pdf(markdown_content=md, title="No Mermaid")
    assert pdf_bytes.startswith(b"%PDF-")
