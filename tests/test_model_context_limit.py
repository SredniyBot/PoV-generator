"""Тесты per-model context limit: домен/стор/сервис/реестр + интеграция
context_service (лимит активной модели заменяет прежний хардкод 2000)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.attachment_service import AttachmentService
from pov_generator.application.context_service import ContextService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.provider_settings_service import ProviderSettingsService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.errors import ValidationError
from pov_generator.domain.llm_settings import (
    DEFAULT_MODEL_CONTEXT_LIMIT,
    default_context_limit,
    resolve_context_limit,
)
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.llm import LLMProviderRegistry
from pov_generator.infrastructure.llm_settings_store import SqliteSettingsStore
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


# --- домен -------------------------------------------------------------------


def test_default_context_limit_by_model_class() -> None:
    # Дефолт = реальное окно модели по семейству.
    assert default_context_limit("claude-sonnet-4-5") == 200_000
    assert default_context_limit("anthropic/claude-opus-4-7") == 200_000
    assert default_context_limit("claude-haiku-4-5") == 200_000
    assert default_context_limit("openai/gpt-4.1-mini") == 1_000_000  # gpt-4.1 раньше прочих
    assert default_context_limit("openai/gpt-4o-mini") == 128_000
    assert default_context_limit("deepseek/deepseek-chat") == 128_000
    assert default_context_limit("неизвестная-модель") == DEFAULT_MODEL_CONTEXT_LIMIT


def test_resolve_context_limit_prefers_stored() -> None:
    assert resolve_context_limit(40000, "claude-haiku-4-5") == 40000
    assert resolve_context_limit(None, "claude-haiku-4-5") == 200_000
    # Мусорное (ниже минимума) → дефолт-окно.
    assert resolve_context_limit(10, "claude-haiku-4-5") == 200_000


# --- стор --------------------------------------------------------------------


def test_store_crud_and_validation(tmp_path: Path) -> None:
    store = SqliteSettingsStore(tmp_path)
    assert store.get_context_limit("m") is None
    store.set_context_limit("claude-sonnet-4-5", 40000)
    assert store.get_context_limit("claude-sonnet-4-5") == 40000
    assert [limit.model_name for limit in store.list_context_limits()] == ["claude-sonnet-4-5"]
    # UPSERT
    store.set_context_limit("claude-sonnet-4-5", 50000)
    assert store.get_context_limit("claude-sonnet-4-5") == 50000
    store.delete_context_limit("claude-sonnet-4-5")
    assert store.get_context_limit("claude-sonnet-4-5") is None
    with pytest.raises(ValidationError):
        store.set_context_limit("m", 10)  # ниже минимума


# --- сервис ------------------------------------------------------------------


def test_service_list_models_exposes_limit(tmp_path: Path) -> None:
    store = SqliteSettingsStore(tmp_path)
    service = ProviderSettingsService(store)
    conn = service.add_connection(provider_type="anthropic", display_name="A", api_key="k")
    # У connection есть seed-routings для известных моделей. Берём первую модель.
    models = service.list_models()
    assert models, "ожидались seed-модели"
    target = models[0]["model_name"]
    entry = next(m for m in models if m["model_name"] == target)
    assert entry["context_limit"] == default_context_limit(target)
    assert entry["context_limit_is_default"] is True
    # Override через сервис.
    service.set_context_limit(model_name=target, context_limit_tokens=33000)
    entry = next(m for m in service.list_models() if m["model_name"] == target)
    assert entry["context_limit"] == 33000
    assert entry["context_limit_is_default"] is False
    # Сброс.
    service.reset_context_limit(target)
    entry = next(m for m in service.list_models() if m["model_name"] == target)
    assert entry["context_limit_is_default"] is True
    del conn


# --- реестр ------------------------------------------------------------------


def test_registry_context_limit_for(tmp_path: Path) -> None:
    store = SqliteSettingsStore(tmp_path)
    reg = LLMProviderRegistry(settings_store=store)
    assert reg.context_limit_for("claude-sonnet-4-5") == 200_000  # дефолт-окно
    store.set_context_limit("claude-sonnet-4-5", 42000)
    assert reg.context_limit_for("claude-sonnet-4-5") == 42000  # сохранённое
    # Без store — всегда дефолт, не падает.
    assert LLMProviderRegistry().context_limit_for("claude-haiku-4-5") == 200_000


# --- интеграция context_service ---------------------------------------------


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    ProjectService(runtime).init_project(
        workspace=workspace,
        name="limit test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Короткий запрос.",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)
    return workspace, runtime, snapshot


def test_interpreter_receives_full_source(tmp_path: Path) -> None:
    """Задача-интерпретатор получает первоисточник (вложение) ЦЕЛИКОМ — без
    прежней обрезки секции состояния (это и есть исправление инцидента РТК)."""
    workspace, runtime, snapshot = _bootstrap(tmp_path)
    service = AttachmentService(runtime)
    big_text = "Важное требование заказчика. " * 800  # ~22k символов
    service.upload(
        workspace,
        runtime.load_project_state(workspace).manifest.project_id,
        filename="brief.txt",
        content=big_text.encode("utf-8"),
        extract_in_background=False,
    )
    # Лист-интерпретатор, объявивший requires.inputs: [attachments] и НЕ
    # требующий апстрим-артефактов: иначе build_for_task справедливо упадёт на
    # отсутствии обязательного входа. Без второго условия выбор leaf'а
    # недетерминирован (несколько шаблонов потребляют attachments; порядок
    # list_tasks при равных created_at — по uuid), и тест флакал.
    leaf = next(
        t
        for t in runtime.list_tasks(workspace)
        if t.template_type == "leaf"
        and "attachments" in snapshot.resolve_template(t.template_ref).inputs.raw_inputs
        and not snapshot.resolve_template(t.template_ref).inputs.required_artifact_roles
    )
    result = ContextService(runtime).build_for_task(
        workspace, snapshot, leaf.task_id, model_context_window=200_000
    )
    sources = [it for it in result.manifest.items if it.title == "Входной файл заказчика"]
    assert sources, "первоисточник должен быть подан интерпретатору"
    # Текст вложения дошёл намного больше прежнего cap'а (8000 символов).
    assert len(sources[0].content) > 15_000
