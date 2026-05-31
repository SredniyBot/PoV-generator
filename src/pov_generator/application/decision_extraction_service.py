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
    DecisionAlternative,
    normalized_decision_title_key,
    strip_decision_category_prefix,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime

logger = logging.getLogger(__name__)


def decisions_schema() -> dict[str, Any]:
    """JSON-schema массива самоотчётных решений.

    Встраивается ExecutionService как поле ``decisions`` объединённой схемы
    генерации: модель вместе с артефактом перечисляет имплицитные проектные
    решения, которые она приняла при сборке. Поле опциональное — пустой
    массив, если ярких выборов нет.
    """
    return {
        "type": "array",
        "maxItems": 3,
        "items": {
            "type": "object",
            "required": [
                "title",
                "description",
                "category",
                "alternatives",
                "chosen_in_artifact_option_id",
                "rationale",
                "level",
                "confidence",
            ],
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string", "enum": list(DECISION_CATEGORIES)},
                "alternatives": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "required": ["option_id", "label", "description", "confidence"],
                        "additionalProperties": False,
                        "properties": {
                            "option_id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "pros": {"type": "array", "items": {"type": "string"}},
                            "cons": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        },
                    },
                },
                "chosen_in_artifact_option_id": {"type": "string"},
                "rationale": {"type": "string"},
                "level": {"type": "string", "enum": ["business", "architecture", "detail"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
        },
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
        "заказчика, оформление документа. Если ярких выборов нет — верни "
        "пустой массив. От 0 до 3 на артефакт; лучше 0, чем шум.\n"
        "Для каждого: title (3-7 слов), description (1-2 предложения), "
        "category (из enum), alternatives (2-4 реальных варианта с label/"
        "description/confidence), chosen_in_artifact_option_id (что зашито в "
        "артефакте), rationale, level (business/architecture/detail), "
        "confidence (0..1).\n"
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
        raw_alts = raw.get("alternatives") or []
        alternatives = tuple(
            DecisionAlternative(
                option_id=str(alt["option_id"]),
                label=str(alt.get("label", "")),
                description=str(alt.get("description", "")),
                pros=tuple(alt.get("pros") or ()),
                cons=tuple(alt.get("cons") or ()),
                confidence=(
                    float(alt["confidence"]) if alt.get("confidence") is not None else None
                ),
            )
            for alt in raw_alts
            if isinstance(alt, dict) and "option_id" in alt
        )
        if len(alternatives) < 2:
            raise ValueError("self-reported decision: need >= 2 alternatives")
        if any(alt.confidence is None for alt in alternatives):
            raise ValueError("self-reported decision: confidence required on alts")

        category = str(raw.get("category") or "").strip()
        if category not in DECISION_CATEGORIES:
            raise ValueError(f"self-reported decision: bad category {category!r}")

        chosen = str(raw.get("chosen_in_artifact_option_id") or "")
        if chosen not in {alt.option_id for alt in alternatives}:
            chosen = alternatives[0].option_id

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
