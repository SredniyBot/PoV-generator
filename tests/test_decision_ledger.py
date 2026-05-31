"""Тесты для реестра решений (v3.0).

Покрывают доменную модель `Decision` и хранилище в `SqliteRuntime`.

Не покрывают (это другие фазы):
- pre-flight планирование в ExecutionService (Фаза 2);
- read API + REST endpoint (отдельный тест-файл `test_decisions_api.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.common.errors import NotFoundError
from pov_generator.domain.decisions import (
    ENGAGEMENT_LEVELS,
    Decision,
    DecisionAlternative,
    levels_for_mode,
    normalized_decision_signature,
    normalized_decision_title_key,
    should_surface_to_user,
)
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

# ---------------------------------------------------------------------------
# Доменная модель
# ---------------------------------------------------------------------------


def _make_decision(
    *,
    decision_id: str = "d-1",
    project_id: str = "p-1",
    title: str = "Выбор СУБД",
    description: str = "Какую СУБД использовать для основного сервиса",
    category: str = "tech_stack",
    level: str = "architecture",
    confidence: float = 0.8,
    chosen: str = "opt-postgres",
) -> Decision:
    return Decision(
        decision_id=decision_id,
        project_id=project_id,
        title=title,
        description=description,
        category=category,
        chosen_option_id=chosen,
        alternatives=(
            DecisionAlternative(
                option_id="opt-postgres",
                label="PostgreSQL",
                description="Реляционная, transactions, partial indexes",
                pros=("ACID", "богатый SQL"),
                cons=("оверхед на простые таблицы",),
                confidence=0.85,
            ),
            DecisionAlternative(
                option_id="opt-mongo",
                label="MongoDB",
                description="Документная",
                pros=("гибкая схема",),
                cons=("слабее с join'ами",),
                confidence=0.4,
            ),
        ),
        rationale="В контексте требования к реляционным запросам — PostgreSQL естественный выбор",
        level=level,  # type: ignore[arg-type]
        level_rationale="Решение затрагивает несколько компонентов; обратимо только с миграцией данных",
        confidence=confidence,
        status="proposed",
        source="identification",
        source_task_id="task-arch-1",
        affected_artifact_ids=("artifact-arch-design",),
        depends_on_decision_ids=(),
    )


def test_chosen_alternative_resolves_to_correct_option() -> None:
    decision = _make_decision()
    chosen = decision.chosen_alternative
    assert chosen is not None
    assert chosen.option_id == "opt-postgres"
    assert chosen.label == "PostgreSQL"


def test_chosen_alternative_returns_none_when_id_does_not_match() -> None:
    """Защитное поведение: если chosen_option_id пуст или указывает на
    несуществующий вариант — возвращается None, а не выбрасывает."""
    decision = _make_decision(chosen="opt-does-not-exist")
    assert decision.chosen_alternative is None


def test_effective_level_uses_user_override_when_present() -> None:
    decision = _make_decision(level="detail")
    assert decision.effective_level == "detail"

    overridden = Decision(**{**decision.__dict__, "free_form_level_override": "architecture"})
    assert overridden.effective_level == "architecture"


def test_is_low_confidence_threshold_at_0_5() -> None:
    """Порог 0.5 — эмпирическая граница для подсветки рискованного."""
    assert _make_decision(confidence=0.49).is_low_confidence is True
    assert _make_decision(confidence=0.51).is_low_confidence is False
    # Граница строгая: 0.5 не считается «низкой».
    assert _make_decision(confidence=0.5).is_low_confidence is False


def test_was_user_modified_true_only_on_real_override() -> None:
    base = _make_decision()
    assert base.was_user_modified is False
    # Через статус
    overridden = Decision(**{**base.__dict__, "status": "user_overridden"})
    assert overridden.was_user_modified is True
    # Через user_action
    via_action = Decision(**{**base.__dict__, "user_action": "modified"})
    assert via_action.was_user_modified is True


def test_manual_choice_clears_low_confidence_flag() -> None:
    """v3.9: если вариант выбрал пользователь вручную — это подтверждение,
    флаг «система не уверена» снимается, даже при низкой уверенности LLM."""
    low = _make_decision(confidence=0.2)
    assert low.is_low_confidence is True
    # Ручной override статусом
    overridden = Decision(**{**low.__dict__, "status": "user_overridden"})
    assert overridden.is_low_confidence is False
    # Ручной выбор через user_action
    via_action = Decision(**{**low.__dict__, "user_action": "modified"})
    assert via_action.is_low_confidence is False


def test_normalized_signature_uses_explicit_category() -> None:
    decision = _make_decision(title="Выбор   СУБД?", category="tech_stack")

    signature = normalized_decision_signature(decision)

    assert signature.decision_id == decision.decision_id
    assert signature.normalized_title_key == "выбор-субд"
    assert signature.category == "tech_stack"
    assert signature.chosen_answer_summary == "PostgreSQL: Реляционная, transactions, partial indexes"
    assert signature.status == "proposed"
    assert decision.description_without_category == "Какую СУБД использовать для основного сервиса"


def test_normalized_signature_falls_back_to_legacy_description_prefix() -> None:
    decision = _make_decision(
        description="[scope] Что включить в пилотный этап",
        category="",
    )

    signature = normalized_decision_signature(decision)

    assert decision.normalized_category == "scope"
    assert decision.description_without_category == "Что включить в пилотный этап"
    assert signature.category == "scope"


def test_normalized_title_key_is_stable_for_unicode_variants() -> None:
    assert normalized_decision_title_key("Выбор   СУБД?") == "выбор-субд"
    assert normalized_decision_title_key("Ёмкость MVP") == "емкость-mvp"
    assert normalized_decision_title_key("ＡＰＩ формат") == "api-формат"


def test_normalized_title_key_handles_empty_and_very_long_titles() -> None:
    assert normalized_decision_title_key("") == "untitled"
    assert normalized_decision_title_key("!!!").startswith("untitled-")

    key = normalized_decision_title_key(" ".join(f"word{i}" for i in range(80)))
    assert len(key) <= 120
    assert key.rsplit("-", 1)[-1]


# ---------------------------------------------------------------------------
# Кумулятивные режимы (CE17)
# ---------------------------------------------------------------------------


def test_engagement_levels_are_cumulative() -> None:
    """expert ⊇ control ⊇ balanced ⊇ autopilot.

    Это ключевой инвариант v3.0: смена режима «вверх» только добавляет
    уровни, никогда не вычитает."""
    autopilot = levels_for_mode("autopilot")
    balanced = levels_for_mode("balanced")
    control = levels_for_mode("control")
    expert = levels_for_mode("expert")

    assert autopilot <= balanced
    assert balanced <= control
    assert control <= expert


def test_autopilot_surfaces_nothing() -> None:
    """В autopilot никакое решение не должно попасть в checkpoint —
    это и есть смысл «опасного режима»."""
    for level in ("business", "architecture", "detail"):
        d = _make_decision(level=level)
        assert should_surface_to_user(d, "autopilot") is False


def test_expert_surfaces_all_three_levels() -> None:
    for level in ("business", "architecture", "detail"):
        d = _make_decision(level=level)
        assert should_surface_to_user(d, "expert") is True


def test_balanced_surfaces_only_business() -> None:
    assert should_surface_to_user(_make_decision(level="business"), "balanced") is True
    assert should_surface_to_user(_make_decision(level="architecture"), "balanced") is False
    assert should_surface_to_user(_make_decision(level="detail"), "balanced") is False


def test_control_surfaces_business_plus_architecture() -> None:
    assert should_surface_to_user(_make_decision(level="business"), "control") is True
    assert should_surface_to_user(_make_decision(level="architecture"), "control") is True
    assert should_surface_to_user(_make_decision(level="detail"), "control") is False


def test_should_surface_respects_user_reclassification() -> None:
    """Если пользователь переклассифицировал решение — фильтр
    использует effective_level, не raw level."""
    base = _make_decision(level="detail")
    # В control сырое 'detail' не surfaces
    assert should_surface_to_user(base, "control") is False
    # Но если пользователь сказал «это на самом деле architecture» — surfaces
    reclassified = Decision(**{**base.__dict__, "free_form_level_override": "architecture"})
    assert should_surface_to_user(reclassified, "control") is True


def test_unknown_mode_falls_back_to_balanced() -> None:
    """Защита от рассинхрона UI и backend: неизвестный режим не должен
    приводить к крашу или сюрпризу (никаких решений / все)."""
    assert levels_for_mode("unknown-mode") == ENGAGEMENT_LEVELS["balanced"]


# ---------------------------------------------------------------------------
# Хранилище — round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime() -> SqliteRuntime:
    return SqliteRuntime()


def test_upsert_and_get_decision_round_trip(tmp_path: Path, runtime: SqliteRuntime) -> None:
    """Полный обход данных: insert → get → все поля сохранились."""
    workspace = tmp_path / "ws"
    decision = _make_decision()
    saved = runtime.upsert_decision(workspace, decision)
    assert saved.created_at  # timestamp проставлен
    assert saved.updated_at == saved.created_at

    fetched = runtime.get_decision(workspace, decision.decision_id)
    assert fetched.title == "Выбор СУБД"
    assert fetched.level == "architecture"
    assert len(fetched.alternatives) == 2
    assert fetched.alternatives[0].option_id == "opt-postgres"
    assert fetched.alternatives[0].pros == ("ACID", "богатый SQL")
    assert fetched.chosen_option_id == "opt-postgres"
    assert fetched.confidence == 0.8
    assert fetched.affected_artifact_ids == ("artifact-arch-design",)
    assert fetched.category == "tech_stack"


def test_upsert_normalizes_legacy_category_prefix(tmp_path: Path, runtime: SqliteRuntime) -> None:
    workspace = tmp_path / "ws"
    decision = _make_decision(
        decision_id="legacy-category",
        description="[scope] Что входит в MVP",
        category="",
    )

    runtime.upsert_decision(workspace, decision)
    fetched = runtime.get_decision(workspace, "legacy-category")

    assert fetched.category == "scope"
    assert fetched.description == "Что входит в MVP"
    assert fetched.normalized_category == "scope"


def test_upsert_updates_existing_decision(tmp_path: Path, runtime: SqliteRuntime) -> None:
    """Повторный upsert с тем же id — обновляет, не дублирует.

    Важный поведенческий контракт: pre-flight LLM может перепланировать
    одно и то же решение, мы хотим хранить только последнюю версию."""
    workspace = tmp_path / "ws"
    original = _make_decision(confidence=0.6)
    runtime.upsert_decision(workspace, original)

    # Меняем уверенность и переоцениваем
    updated = Decision(**{**original.__dict__, "confidence": 0.9, "status": "locked_in"})
    runtime.upsert_decision(workspace, updated)

    # Должна остаться одна запись с новыми значениями
    assert runtime.count_decisions(workspace, project_id="p-1") == 1
    fetched = runtime.get_decision(workspace, "d-1")
    assert fetched.confidence == 0.9
    assert fetched.status == "locked_in"


def test_get_decision_raises_not_found(tmp_path: Path, runtime: SqliteRuntime) -> None:
    workspace = tmp_path / "ws"
    # Сначала создаём базу — иначе sqlite не создастся и таблиц не будет
    runtime.upsert_decision(workspace, _make_decision())
    with pytest.raises(NotFoundError, match="decision .* не найдено"):
        runtime.get_decision(workspace, "does-not-exist")


def test_list_decisions_filters_by_level(tmp_path: Path, runtime: SqliteRuntime) -> None:
    workspace = tmp_path / "ws"
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-biz", level="business"))
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-arch", level="architecture"))
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-det", level="detail"))

    biz_only = runtime.list_decisions(workspace, project_id="p-1", level="business")
    assert [d.decision_id for d in biz_only] == ["d-biz"]

    arch_only = runtime.list_decisions(workspace, project_id="p-1", level="architecture")
    assert [d.decision_id for d in arch_only] == ["d-arch"]


def test_list_decisions_filters_by_status(tmp_path: Path, runtime: SqliteRuntime) -> None:
    workspace = tmp_path / "ws"
    runtime.upsert_decision(
        workspace,
        Decision(**{**_make_decision(decision_id="proposed").__dict__, "status": "proposed"}),
    )
    runtime.upsert_decision(
        workspace,
        Decision(**{**_make_decision(decision_id="locked").__dict__, "status": "locked_in"}),
    )

    locked = runtime.list_decisions(workspace, project_id="p-1", status="locked_in")
    assert [d.decision_id for d in locked] == ["locked"]


def test_list_decisions_returns_in_creation_order(tmp_path: Path, runtime: SqliteRuntime) -> None:
    """Решения упорядочены по времени появления — UI рисует в хронологии
    работы LLM, это даёт пользователю осмысленный нарратив."""
    workspace = tmp_path / "ws"
    for idx in range(5):
        runtime.upsert_decision(
            workspace,
            _make_decision(decision_id=f"d-{idx}"),
        )
    all_decisions = runtime.list_decisions(workspace, project_id="p-1")
    assert [d.decision_id for d in all_decisions] == ["d-0", "d-1", "d-2", "d-3", "d-4"]


def test_count_decisions_with_and_without_filters(tmp_path: Path, runtime: SqliteRuntime) -> None:
    workspace = tmp_path / "ws"
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-1", level="business"))
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-2", level="architecture"))
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-3", level="architecture"))

    assert runtime.count_decisions(workspace, project_id="p-1") == 3
    assert runtime.count_decisions(workspace, project_id="p-1", level="architecture") == 2
    assert runtime.count_decisions(workspace, project_id="p-1", level="business") == 1
    assert runtime.count_decisions(workspace, project_id="p-1", level="detail") == 0


def test_decisions_are_isolated_per_project(tmp_path: Path, runtime: SqliteRuntime) -> None:
    """Реестр project-scoped — два разных project_id не видят друг друга."""
    workspace = tmp_path / "ws"
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-A", project_id="proj-A"))
    runtime.upsert_decision(workspace, _make_decision(decision_id="d-B", project_id="proj-B"))

    a_only = runtime.list_decisions(workspace, project_id="proj-A")
    assert [d.decision_id for d in a_only] == ["d-A"]

    b_only = runtime.list_decisions(workspace, project_id="proj-B")
    assert [d.decision_id for d in b_only] == ["d-B"]
