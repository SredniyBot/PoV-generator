"""Маршрутизация «класс задачи → модель».

Ось такая: leaf-шаблон объявляет свой класс через ``complexity``
(trivial|standard|complex), а ``resolve_for_purpose("execution", complexity)``
переводит класс в tier-purpose (``execution.trivial`` и т.д.) и берёт
назначенную на него модель. Тесты фиксируют три звена цепочки:

1. маппинг класс → tier-purpose (чистая функция);
2. классификацию шаблонов в реестре (данные не должны молча «съехать»);
3. сквозной резолв: объявленный класс выбирает назначенную модель.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.provider_settings_service import ProviderSettingsService
from pov_generator.domain.llm_settings import (
    PURPOSE_EXECUTION_COMPLEX,
    PURPOSE_EXECUTION_STANDARD,
    PURPOSE_EXECUTION_TRIVIAL,
)
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.llm.registry import (
    LLMProviderRegistry,
    _resolve_purpose_key,
)
from pov_generator.infrastructure.llm_settings_store import SqliteSettingsStore

# Какой класс ожидаем у каждого шаблона. Не перечисляем standard поимённо —
# для него класс не объявляется (по умолчанию None → execution.standard);
# проверяем лишь, что выборочные «средние» задачи остаются дефолтными.
TRIVIAL_TEMPLATES = (
    "common.constraint_inventory",
    "common.glossary_drafting",
    "common.stakeholder_mapping",
)
COMPLEX_TEMPLATES = (
    "common.requirements_spec_generation",
    "architecture.design_synthesis",
)
STANDARD_DEFAULT_TEMPLATES = (
    "common.goal_hypothesis",
    # ТЗ v2: «Разобрать запрос» — теперь анализ (был trivial), класс standard.
    "common.request_normalization",
    "architecture.component_decomposition",
)


# --- 1. Маппинг класс → tier-purpose ----------------------------------------


def test_purpose_key_maps_task_class_to_execution_tier() -> None:
    assert _resolve_purpose_key("execution", "trivial") == PURPOSE_EXECUTION_TRIVIAL
    assert _resolve_purpose_key("execution", "complex") == PURPOSE_EXECUTION_COMPLEX
    assert _resolve_purpose_key("execution", "standard") == PURPOSE_EXECUTION_STANDARD
    # Необъявленный класс (None) — это стандартный tier, а не падение.
    assert _resolve_purpose_key("execution", None) == PURPOSE_EXECUTION_STANDARD
    # Не-execution purposes игнорируют класс.
    assert _resolve_purpose_key("clarification_ce11", "trivial") == "clarification_ce11"


# --- 2. Классификация шаблонов в реестре ------------------------------------


def test_templates_declare_expected_class() -> None:
    snapshot = FilesystemRegistryLoader(Path("templates")).load()

    for ident in TRIVIAL_TEMPLATES:
        template = snapshot.resolve_template(f"{ident}@1.0.0")
        assert template.complexity == "trivial", f"{ident} должен быть trivial"

    for ident in COMPLEX_TEMPLATES:
        template = snapshot.resolve_template(f"{ident}@1.0.0")
        assert template.complexity == "complex", f"{ident} должен быть complex"

    for ident in STANDARD_DEFAULT_TEMPLATES:
        template = snapshot.resolve_template(f"{ident}@1.0.0")
        assert template.complexity is None, f"{ident} должен остаться дефолтным (standard)"


# --- 3. Сквозной резолв по классу -------------------------------------------


def test_resolve_for_purpose_routes_by_task_class(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    store = SqliteSettingsStore(tmp_path)
    service = ProviderSettingsService(store)
    # Подключение с auto-seed routings для известных anthropic-моделей
    # (haiku / sonnet / opus — все маршрутизируемы).
    service.add_connection(
        provider_type="anthropic", display_name="Anthropic", api_key="sk-ant-test"
    )
    service.set_assignment(purpose=PURPOSE_EXECUTION_TRIVIAL, model_name="claude-haiku-4-5")
    service.set_assignment(purpose=PURPOSE_EXECUTION_STANDARD, model_name="claude-sonnet-4-5")
    service.set_assignment(purpose=PURPOSE_EXECUTION_COMPLEX, model_name="claude-opus-4-7")

    registry = LLMProviderRegistry(settings_store=store)

    # Не выходим в сеть: подменяем построение адаптера эхо-объектом,
    # отдающим запрошенную модель.
    class _Echo:
        def __init__(self, model: str) -> None:
            self.model = model
            self.name = "anthropic"

    def _fake_build(connection, *, model, complexity, purpose=None):
        return _Echo(model)

    monkeypatch.setattr(registry, "_build_from_connection", _fake_build)

    # Класс задачи приходит как complexity → выбирается модель назначенного tier.
    assert registry.resolve_for_purpose("execution", complexity="trivial").model == "claude-haiku-4-5"
    assert registry.resolve_for_purpose("execution", complexity="standard").model == "claude-sonnet-4-5"
    assert registry.resolve_for_purpose("execution", complexity="complex").model == "claude-opus-4-7"
    # Необъявленный класс (None) идёт на стандартный tier.
    assert registry.resolve_for_purpose("execution", complexity=None).model == "claude-sonnet-4-5"
