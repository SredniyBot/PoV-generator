"""Post-artifact извлечение неявных проектных решений (v3.6).

ИСТОЧНИК 2 из трёх в подсистеме реестра решений (см. также
:mod:`decision_identification_service` и :mod:`phase_gap_analysis_service`).

ЗАЧЕМ ЭТОТ СЕРВИС СУЩЕСТВУЕТ. Task-level identification (источник 1)
работает на запуске задачи и спрашивает LLM «какие развилки возникают».
LLM хорошо ловит **явные** развилки (длительность пилота, KPI, scope),
но **молчаливо** закладывает в артефакты технические выборы:
конкретные модели LLM, фреймворки, библиотеки, OCR-движки. Эти
выборы никогда не звучат как «решение» — они просто появляются в
тексте артефакта. Пользователь обнаруживает их только когда читает
финальное ТЗ и видит «PostgreSQL» / «Llama-3 8B» / «PaddleOCR» —
и удивляется, почему он эти решения не принимал.

Этот сервис — **страховка**. После каждого сгенерированного primary-
артефакта запускает короткий LLM-pass с двумя входами:

  1. Содержимое только что сгенерированного артефакта (payload).
  2. Title'ы уже существующих в реестре решений.

И спрашивает: «какие *проектные* решения зашиты в этот артефакт, но
которых ещё нет в реестре?»

Найденные сохраняются как ``Decision`` со source="emergent" (отличается
от "pre_flight"), status="accepted_default", без checkpoint. Пользователь
видит их в реестре пост-фактум, может переиграть любое в обычном
порядке override.

ПОЧЕМУ НЕ ДЕЛАТЬ ВСЁ ЧЕРЕЗ EXTRACTION (отказавшись от identification).
Extraction видит **уже принятые** решения, но не может *предупредить* о
проблемных дефолтах ДО сборки артефакта. Identification — это «дай мне
выбор», extraction — это «вот что я уже выбрал, к сведению». Они
комплементарны.

СТОИМОСТЬ. Один LLM-вызов на каждый primary-артефакт. Маленький промпт
(один артефакт, не вся история), маленький output (часто 0-2 решения).
По бюджету в разы дешевле identification. Размер LLM — standard.

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.6.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.errors import ConflictError
from ..common.serialization import utc_now_iso
from ..domain.decisions import Decision, DecisionAlternative
from ..domain.llm_settings import PURPOSE_DECISION_PLANNING
from ..infrastructure.llm import LLMProviderRegistry
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .decision_identification_service import DECISION_CATEGORIES

logger = logging.getLogger(__name__)


_EXTRACTION_COMPLEXITY = "standard"


_EXTRACTION_SYSTEM_PROMPT = """\
<role>
Ты — аналитик, ведущий реестр решений по PoV-проекту. Сейчас ты
читаешь только что сгенерированный артефакт проекта и ищешь в нём
**имплицитные проектные решения**, которые автор артефакта принял
молча, не объявив их как развилки.
</role>

<purpose>
Артефакты PoV (нормализованный запрос, ТЗ, архитектурная карта и т.д.)
часто содержат конкретные технологические и продуктовые выборы — выбор
конкретной СУБД, фреймворка, LLM-модели, библиотеки, OCR-движка,
канала уведомлений, конкретного подразделения для пилота и т.п. Такие
выборы нередко появляются «по умолчанию», без явного обсуждения. Они
должны быть в реестре, чтобы заказчик мог увидеть «а, мы тут зашили
Llama-3, а могли Qwen — это важно».
</purpose>

ЧТО СЧИТАЕМ ИМПЛИЦИТНЫМ ПРОЕКТНЫМ РЕШЕНИЕМ.

ВЫНОСИ (это имплицитное проектное решение):
- В артефакте упомянута конкретная технология / модель / библиотека
  без обоснования альтернатив (PostgreSQL, FAISS, FastAPI, Llama-3,
  Tesseract, etc.).
- В артефакте принят конкретный численный параметр (порог 80%,
  таймаут 5 сек, 50 одновр. пользователей), который мог бы быть
  существенно другим.
- В артефакте зафиксирована конкретная архитектурная развилка
  (монолит vs микросервисы, sync vs async, on-prem vs cloud),
  выбранная без явных альтернатив в тексте.

НЕ ВЫНОСИ:
- Решения, которые уже есть в реестре (даже под другой формулировкой).
- Тривиальные/обратимые детали (имя переменной, цвет UI, формат даты).
- Прямые цитаты из бизнес-запроса заказчика (это не решение, это вход).
- Мета-про-документ (формат раздела, глубина списка, оформление).

ФОРМАТ ВЫВОДА.
Для каждого найденного решения возвращай:
- title: короткое название (3-7 слов).
- description: 1-2 предложения о выборе.
- category: одна из категорий (см. enum). Если ни одна не подходит —
  не выноси.
- alternatives: 2-3 реальных варианта (включая тот, который зашит в
  артефакте, и хотя бы один-два альтернативных). У каждого — label,
  description, confidence (0..1).
- chosen_in_artifact_option_id: id того варианта из alternatives,
  который реально зашит в артефакте. Это будет proposed/chosen.
- rationale: почему именно тот вариант оказался в артефакте (если
  явно сказано — повторить, если нет — короткое объяснение).
- level: business / architecture / detail.
- confidence: 0..1, уверенность что это реально решение (не шум).

КОЛИЧЕСТВО. От 0 до 3 на один артефакт. Лучше 0, чем шум. Если в
артефакте не видно ярких имплицитных выборов — возвращай пустой массив.

Верни ТОЛЬКО валидный JSON по схеме. Без markdown.
"""


def _build_extraction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["decisions"],
        "additionalProperties": False,
        "properties": {
            "decisions": {
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
                        "category": {
                            "type": "string",
                            "enum": list(DECISION_CATEGORIES),
                        },
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
                                    "confidence": {
                                        "type": "number",
                                        "minimum": 0.0,
                                        "maximum": 1.0,
                                    },
                                },
                            },
                        },
                        "chosen_in_artifact_option_id": {"type": "string"},
                        "rationale": {"type": "string"},
                        "level": {
                            "type": "string",
                            "enum": ["business", "architecture", "detail"],
                        },
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                },
            }
        },
    }


@dataclass(frozen=True)
class ExtractionResult:
    """Результат post-artifact extraction.

    Args:
        decisions: вытащенные проектные решения. Все автоматически
            ``accepted_default``, без checkpoint. Привязка к артефакту
            — на вызывающей стороне (через ``affected_artifact_ids``).
        token_usage: usage LLM-вызова.
    """

    decisions: tuple[Decision, ...]
    token_usage: dict[str, Any] = field(default_factory=dict)


class DecisionExtractionService:
    """Извлечение имплицитных проектных решений из готового артефакта."""

    def __init__(
        self,
        runtime: SqliteRuntime,
        *,
        llm_registry: LLMProviderRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._llm = llm_registry or LLMProviderRegistry()

    def extract_from_artifact(
        self,
        *,
        workspace: Path,
        project_id: str,
        artifact_id: str,
        artifact_role: str,
        artifact_content: str,
        task_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        token_usage_out: dict[str, int] | None = None,
    ) -> tuple[Decision, ...]:
        """Извлечь имплицитные проектные решения из артефакта.

        Сохраняет в реестр и возвращает upserted Decision'ы. Не вызывает
        checkpoint (все идут как ``accepted_default``).

        v3.8.4: принимает уже **отрендеренный текст** артефакта (обычно
        markdown через :func:`render_markdown`), а не сырой JSON. Markdown —
        это смысловое сжатое представление содержимого артефакта без
        технического шума (имена полей, кавычки, escape-последовательности,
        нулевые поля). Для крупных артефактов это даёт −40-60% размера
        промпта в input-токенах при той же информации для LLM.

        Универсальность: artifact-specific markdown-рендереры уже
        существуют для всех ролей через
        :func:`artifact_contracts.render_markdown`; никаких per-role
        выжимок здесь городить не надо. Если вызывающий код не смог
        отрендерить markdown (rare — KeyError из-за неполного payload),
        он передаёт сюда сырой JSON-снимок — extraction всё равно
        отработает, просто на более громоздком входе.

        Args:
            workspace: путь workspace проекта.
            project_id: id проекта.
            artifact_id: id артефакта (для привязки).
            artifact_role: роль артефакта (для контекста промпта).
            artifact_content: текстовое представление содержимого
                артефакта (markdown / plain text / fallback JSON).
            task_id: задача, породившая артефакт (для source_task_id).
            provider/model: override (тесты).
            token_usage_out: если передан — заполняется usage'ом
                (input/output/total tokens).

        Returns:
            Tuple созданных Decision-объектов. Может быть пустым.
        """
        # Подгружаем существующие title'ы — для антидублирующей
        # инструкции в промпте.
        existing = self._runtime.list_decisions(workspace, project_id=project_id)
        existing_titles = tuple(d.title for d in existing)

        llm = self._resolve_llm(provider=provider, model=model)

        user_prompt = self._build_user_prompt(
            artifact_role=artifact_role,
            artifact_content=artifact_content,
            existing_titles=existing_titles,
        )
        schema = _build_extraction_schema()

        try:
            response = llm.chat_json(
                system_prompt=_EXTRACTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(
                f"Ошибка post-artifact extraction через {llm.name}: {exc}"
            ) from exc

        usage = getattr(llm, "last_usage", None)
        if usage is not None and token_usage_out is not None:
            token_usage_out.update(
                {
                    "input_tokens": int(usage.input_tokens),
                    "output_tokens": int(usage.output_tokens),
                    "cache_read_tokens": int(usage.cache_read_tokens),
                    "cache_write_tokens": int(usage.cache_write_tokens),
                    "total_tokens": int(usage.total_tokens),
                }
            )

        decisions = self._build_decisions(
            response=response,
            project_id=project_id,
            task_id=task_id,
            artifact_id=artifact_id,
        )

        # Сохраняем сразу же. Все extracted-решения идут как
        # accepted_default — пользователь не блокируется, но видит их.
        saved: list[Decision] = []
        for d in decisions:
            try:
                self._runtime.upsert_decision(workspace, d)
                saved.append(self._runtime.get_decision(workspace, d.decision_id))
            except Exception:  # noqa: BLE001
                # Не валим всё из-за одной битой записи.
                continue
        return tuple(saved)

    # ---- helpers ---------------------------------------------------------

    def _resolve_llm(self, *, provider: str | None, model: str | None):
        if provider is not None:
            return self._llm.get(
                provider=provider, model=model, complexity=_EXTRACTION_COMPLEXITY
            )
        try:
            return self._llm.resolve_for_purpose(
                PURPOSE_DECISION_PLANNING,
                complexity=_EXTRACTION_COMPLEXITY,
                override_model=model,
            )
        except ConflictError:
            return self._llm.resolve_for_purpose(
                "execution",
                complexity="standard",
                override_model=model,
            )

    def _build_user_prompt(
        self,
        *,
        artifact_role: str,
        artifact_content: str,
        existing_titles: tuple[str, ...],
    ) -> str:
        # Лимитируем 60 последних title'ов чтобы промпт не разрастался.
        recent = existing_titles[-60:]
        registry_block = ""
        if recent:
            bullets = "\n".join(f"- {t}" for t in recent)
            registry_block = (
                f"### Уже в реестре ({len(existing_titles)} решений; "
                f"показаны последние {len(recent)})\n"
                f"{bullets}\n\n"
                f"НЕ дублируй эти решения, даже если формулировка отличается.\n\n"
            )

        return (
            f"### Артефакт\n"
            f"**Роль:** {artifact_role}\n\n"
            f"{registry_block}"
            f"### Содержимое артефакта\n"
            f"{artifact_content}\n\n"
            f"### Запрос\n"
            f"Вытащи из артефакта **имплицитные проектные решения**, которых "
            f"ещё нет в реестре. От 0 до 3 — лучше 0, чем шум."
        )

    def _build_decisions(
        self,
        *,
        response: dict[str, Any],
        project_id: str,
        task_id: str | None,
        artifact_id: str,
    ) -> tuple[Decision, ...]:
        raw_decisions = response.get("decisions") or []
        if not isinstance(raw_decisions, list):
            return ()
        now = utc_now_iso()
        out: list[Decision] = []
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                continue
            try:
                d = self._build_single(
                    raw=raw,
                    project_id=project_id,
                    task_id=task_id,
                    artifact_id=artifact_id,
                    now=now,
                )
            except (KeyError, TypeError, ValueError):
                continue
            out.append(d)
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
                    float(alt["confidence"])
                    if alt.get("confidence") is not None
                    else None
                ),
            )
            for alt in raw_alts
            if isinstance(alt, dict) and "option_id" in alt
        )
        if len(alternatives) < 2:
            raise ValueError("extracted decision: need >= 2 alternatives")
        if any(alt.confidence is None for alt in alternatives):
            raise ValueError("extracted decision: confidence required on alts")

        category = str(raw.get("category") or "").strip()
        if category not in DECISION_CATEGORIES:
            raise ValueError(f"extracted decision: bad category {category!r}")

        chosen = str(raw.get("chosen_in_artifact_option_id") or "")
        if chosen not in {alt.option_id for alt in alternatives}:
            chosen = alternatives[0].option_id

        level = raw.get("level")
        if level not in ("business", "architecture", "detail"):
            level = "architecture"

        description = str(raw.get("description") or "")
        if not description.startswith("[") or "]" not in description[:30]:
            description = f"[{category}] {description}"

        # source="emergent" — этот enum уже существует в v3.0 для
        # незапланированных решений, возникших по ходу генерации.
        # Идеально подходит под extraction.
        return Decision(
            decision_id=str(uuid.uuid4()),
            project_id=project_id,
            title=str(raw.get("title") or "Untitled extracted decision"),
            description=description,
            chosen_option_id=chosen,
            alternatives=alternatives,
            rationale=str(raw.get("rationale") or ""),
            level=level,  # type: ignore[arg-type]
            level_rationale="Вытащено пост-фактум из артефакта (источник: extraction).",
            confidence=float(raw.get("confidence") or 0.5),
            # Auto-accepted: это уже зафиксировано в артефакте, мы только
            # отражаем в реестре. Пользователь может переиграть в любой
            # момент через override.
            status="accepted_default",
            user_action="not_shown",
            source="emergent",
            source_task_id=task_id,
            affected_artifact_ids=(artifact_id,),
            created_at=now,
            updated_at=now,
        )
