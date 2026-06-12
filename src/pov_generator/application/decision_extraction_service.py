"""Самоотчётные эмерджентные решения из ответа генерации (v3.10, идея А).

ИСТОЧНИК 2 из подсистемы реестра решений (см. также
:mod:`decision_identification_service` — выявление решений ДО сборки).

ЗАЧЕМ. Выявление до сборки (источник 1) спрашивает модель «какие развилки
предстоят» и при необходимости останавливает задачу для участия человека.
Оно хорошо ловит **явные** развилки (длительность пилота, KPI, scope), но
модель **молчаливо** закладывает в артефакт технические выборы: конкретные
СУБД, фреймворки, LLM-модели, OCR-движки. Эти выборы не звучат как
«решение» — они просто появляются в тексте, и заказчик обнаруживает их
постфактум.

КАК ЭТО РАБОТАЕТ ТЕПЕРЬ. Раньше после сборки шёл ОТДЕЛЬНЫЙ LLM-вызов,
который перечитывал готовый артефакт и вытаскивал из него имплицитные
решения. Идея А (v3.10) убрала этот вызов: модель, которая собирала
артефакт, в ТОМ ЖЕ ответе возвращает поле ``decisions`` со списком принятых
ею решений — она знает их точнее любого «перечитывателя», и это экономит и
сам вызов, и повторную отправку всего артефакта на вход.

РОЛЬ ЭТОГО СЕРВИСА. Принять «сырые» решения из ответа генерации, отсеять
дубли (то, что уже есть в реестре — в т.ч. переданные в промпт сборки как
ограничения), собрать доменные :class:`Decision` и сохранить как
``accepted_default`` (source = identification-независимый ``emergent``).
Пользователь видит их в реестре постфактум и может переиграть override'ом.
Сервис LLM НЕ вызывает — только парсит и персистит (чистый SRP).

Схему массива решений (:func:`decisions_schema`) встраивает в combined-схему
генерации ExecutionService.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ..common.serialization import utc_now_iso
from ..domain.decisions import (
    DECISION_CATEGORIES,
    SOURCE_EMERGENT,
    Decision,
    light_decision_item_schema,
    normalized_decision_title_key,
    strip_decision_category_prefix,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .decision_light_parsing import light_alternatives, resolve_recommended_option_id

logger = logging.getLogger(__name__)


def decisions_schema() -> dict[str, Any]:
    """JSON-schema массива самоотчётных решений.

    Встраивается ExecutionService как поле ``decisions`` объединённой схемы
    генерации: модель вместе с артефактом перечисляет имплицитные проектные
    решения, которые она приняла при сборке. Поле опциональное — пустой
    массив, если ярких выборов нет.
    """
    # ЕДИНАЯ облегчённая схема решения (общая с identification-путём) — чтобы
    # решения в реестре были согласованны независимо от источника.
    return {
        "type": "array",
        "maxItems": 3,
        "items": light_decision_item_schema(),
    }


def decisions_instruction() -> str:
    """Инструкция для модели: как заполнять поле ``decisions`` при сборке.

    Добавляется к system-промпту генерации, когда поле решений запрошено.
    """
    return (
        "<decisions_self_report>\n"
        "Собирая артефакт, ты неизбежно принимаешь имплицитные проектные "
        "решения: конкретные СУБД, фреймворки, LLM-модели, библиотеки, "
        "численные пороги, архитектурные развилки (монолит/микросервисы, "
        "sync/async, on-prem/cloud). Перечисли такие принятые тобой выборы в "
        "поле `decisions` — чтобы они попали в реестр решений, а не остались "
        "молча зашитыми в текст.\n"
        "ВЫНОСИ только содержательные выборы, которые могли бы быть иными. "
        "НЕ выноси: тривиальные/обратимые детали, прямые цитаты из запроса "
        "заказчика, оформление документа. НЕ выноси коммерческие / юридические / "
        "организационные вопросы (бюджет, сроки, права на результаты, гарантии, "
        "поддержка, подписанты, состав команды) — их решают вне системы. Если "
        "ярких выборов нет — верни пустой массив. От 0 до 3 на артефакт; лучше "
        "0, чем шум.\n"
        "Для каждого: title (3-7 слов), description (1-2 предложения), "
        "category (из enum), alternatives (2-4 реальных варианта, каждый с "
        "label и description), recommended (label того варианта, что зашит в "
        "артефакте — точно из alternatives), rationale, level "
        "(business/architecture/detail).\n"
        "</decisions_self_report>"
    )


class DecisionExtractionService:
    """Персистер самоотчётных эмерджентных решений (без вызовов LLM).

    Принимает «сырые» решения из ответа генерации, дедуплицирует относительно
    реестра и сохраняет как ``accepted_default``. Не зависит от
    LLM-провайдера — это чистая трансформация + персистентность.
    """

    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def persist_self_reported(
        self,
        workspace: Path,
        *,
        project_id: str,
        artifact_id: str,
        task_id: str | None,
        raw_decisions: list[Any],
    ) -> tuple[Decision, ...]:
        """Сохранить самоотчётные решения из ответа генерации.

        Дедупликация по нормализованному заголовку относительно ВСЕГО реестра
        проекта — чтобы не задвоить решения, уже принятые до сборки и
        переданные в промпт как ограничения. Все сохраняются как
        ``accepted_default`` (пользователь не блокируется, но видит их).

        Returns: сохранённые Decision'ы (может быть пусто).
        """
        if not raw_decisions:
            return ()
        existing = self._runtime.list_decisions(workspace, project_id=project_id)
        seen_keys = {normalized_decision_title_key(d.title) for d in existing}

        built = self._build_decisions(
            raw_decisions=raw_decisions,
            project_id=project_id,
            task_id=task_id,
            artifact_id=artifact_id,
        )
        saved: list[Decision] = []
        for decision in built:
            key = normalized_decision_title_key(decision.title)
            if key in seen_keys:
                continue  # уже в реестре — не дублируем
            try:
                self._runtime.upsert_decision(workspace, decision)
                saved.append(self._runtime.get_decision(workspace, decision.decision_id))
                seen_keys.add(key)
            except Exception:  # noqa: BLE001 — одна битая запись не валит остальные
                continue
        return tuple(saved)

    # ---- helpers ---------------------------------------------------------

    def _build_decisions(
        self,
        *,
        raw_decisions: list[Any],
        project_id: str,
        task_id: str | None,
        artifact_id: str,
    ) -> tuple[Decision, ...]:
        if not isinstance(raw_decisions, list):
            return ()
        now = utc_now_iso()
        out: list[Decision] = []
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                continue
            try:
                out.append(
                    self._build_single(
                        raw=raw,
                        project_id=project_id,
                        task_id=task_id,
                        artifact_id=artifact_id,
                        now=now,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(out)

    def _build_single(
        self,
        *,
        raw: dict[str, Any],
        project_id: str,
        task_id: str | None,
        artifact_id: str,
        now: str,
    ) -> Decision:
        # Облегчённая схема (alternatives={label, description}) → богатый domain
        # через ОБЩИЙ маппинг, единый с identification-путём: option_id=opt-N,
        # pros/cons пустые, confidence=None. Источники решений однородны.
        alternatives = light_alternatives(raw.get("alternatives"))
        if len(alternatives) < 2:
            raise ValueError("self-reported decision: need >= 2 alternatives")

        category = str(raw.get("category") or "").strip()
        if category not in DECISION_CATEGORIES:
            raise ValueError(f"self-reported decision: bad category {category!r}")

        chosen = resolve_recommended_option_id(alternatives, raw.get("recommended"))

        level = raw.get("level")
        if level not in ("business", "architecture", "detail"):
            level = "architecture"

        description = strip_decision_category_prefix(str(raw.get("description") or ""))

        return Decision(
            decision_id=str(uuid.uuid4()),
            project_id=project_id,
            title=str(raw.get("title") or "Untitled decision"),
            description=description,
            category=category,
            chosen_option_id=chosen,
            alternatives=alternatives,
            rationale=str(raw.get("rationale") or ""),
            level=level,  # type: ignore[arg-type]
            level_rationale="Указано генерацией как принятое при сборке артефакта.",
            confidence=float(raw.get("confidence") or 0.5),
            # Auto-accepted: уже зафиксировано в артефакте, отражаем в реестре.
            # Пользователь может переиграть override'ом в любой момент.
            status="accepted_default",
            user_action="not_shown",
            source=SOURCE_EMERGENT,
            source_task_id=task_id,
            affected_artifact_ids=(artifact_id,),
            created_at=now,
            updated_at=now,
        )
