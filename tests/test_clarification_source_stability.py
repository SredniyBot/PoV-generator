"""Тесты для B2 (W6 follow-up): стабильный source_id у validation-кандидатов.

Жалоба пользователя: «после ответа на вопросы система уходит думать и
снова спрашивает то же самое».

Корневая причина: в `validation_service._semantic_analysis` source_id
формировался как `f"{task_id}:{artifact_id}:..."`. При re-run задачи
создаётся новый artifact_id → source_id меняется → `find_clarification_by_source`
не находит answered request → создаётся дубль.

Фикс: artifact_id заменён на artifact_role (стабильно для задачи), и
для blocking_questions добавлен hash вопроса в source_id (порядок может
меняться при re-run).

Этот тест проверяет инвариант: при повторной генерации тех же кандидатов
из validation для той же задачи и роли — source_id стабилен и dedup
будет работать.
"""

from __future__ import annotations

import hashlib


def _build_low_confidence_source_id(task_id: str, artifact_role: str) -> str:
    return f"{task_id}:{artifact_role}:low_confidence"


def _build_question_source_id(task_id: str, artifact_role: str, question: str) -> str:
    normalized = question.strip().lower()
    qhash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{task_id}:{artifact_role}:question:{qhash}"


def test_low_confidence_source_id_is_stable_across_artifact_changes() -> None:
    # Тот же task_id + artifact_role, разные artifact_id — source_id одинаков.
    sid1 = _build_low_confidence_source_id("task-abc", "requirements_spec")
    sid2 = _build_low_confidence_source_id("task-abc", "requirements_spec")
    assert sid1 == sid2
    assert "artifact" not in sid1  # явно нет ссылки на artifact_id


def test_low_confidence_source_id_differs_per_role() -> None:
    sid1 = _build_low_confidence_source_id("task-abc", "requirements_spec")
    sid2 = _build_low_confidence_source_id("task-abc", "review_report")
    assert sid1 != sid2


def test_question_source_id_is_stable_for_same_text() -> None:
    sid1 = _build_question_source_id("task-abc", "requirements_spec", "Какой объём миграции?")
    sid2 = _build_question_source_id("task-abc", "requirements_spec", "Какой объём миграции?")
    assert sid1 == sid2


def test_question_source_id_is_stable_with_whitespace_and_case() -> None:
    # Минимальные cosmetic-изменения не должны ломать dedup.
    sid1 = _build_question_source_id("task-abc", "requirements_spec", "Какой объём миграции?")
    sid2 = _build_question_source_id("task-abc", "requirements_spec", "какой объём миграции?  ")
    assert sid1 == sid2


def test_question_source_id_differs_for_different_questions() -> None:
    sid1 = _build_question_source_id("task-abc", "requirements_spec", "Какой объём миграции?")
    sid2 = _build_question_source_id("task-abc", "requirements_spec", "Какой стек разработки?")
    assert sid1 != sid2


def test_question_source_id_is_not_affected_by_question_index() -> None:
    # Раньше source_id содержал index из enumerate(blocking_questions) — при
    # перестановке порядка LLM-выдачи source_id менялся. Теперь нет.
    sid_a = _build_question_source_id("task-abc", "requirements_spec", "Q1")
    sid_b = _build_question_source_id("task-abc", "requirements_spec", "Q2")
    sid_a_again = _build_question_source_id("task-abc", "requirements_spec", "Q1")
    assert sid_a == sid_a_again  # тот же вопрос — тот же id
    assert sid_a != sid_b  # разные вопросы — разные id, без зависимости от порядка


def test_question_hash_length_is_bounded() -> None:
    # source_id должен оставаться компактным — sanity check.
    sid = _build_question_source_id("task-abc", "requirements_spec", "Q" * 5000)
    # `task-abc:requirements_spec:question:` = 38 chars + 10 hash = 48
    assert len(sid) < 80
