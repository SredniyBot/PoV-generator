"""Тесты для PDF-экспорта артефактов.

Покрывают:
- pdf_export.render_artifact_pdf — конвертация markdown → PDF (bytes).
- /api/projects/{id}/artifacts/{aid}/download.pdf endpoint.

Тестируем структуру/инварианты, а не точное содержание — внутреннее
устройство PDF (offsets, font tables) зависит от reportlab версии.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from test_m9_api import init_project, run_stub_workflow  # type: ignore

from pov_generator.application.pdf_export import render_artifact_pdf
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
