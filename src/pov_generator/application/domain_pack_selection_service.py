"""Авто-выбор доменных пакетов под бизнес-запрос.

Два режима:

* ``stub`` — детерминированный matcher по ``entry_signals`` пака. Используется
  в тестах и при ``POV_DOMAIN_PACK_SELECTION_PROVIDER=stub``. Не делает
  LLM-вызовов, поэтому идёт собственным путём.

* LLM (openrouter / claude_sdk / claude_subscription) — реальный выбор через
  языковую модель. Конкретный провайдер резолвится через
  :class:`LLMProviderRegistry`, switch по имени провайдера живёт только там.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..common.errors import ConflictError
from ..domain.registry import DomainPackSpec, ObjectRef, RegistrySnapshot
from ..infrastructure.llm import LLMProviderRegistry

# Селектор domain pack'ов — это «standard»-сложность: понять запрос,
# выбрать минимум-достаточный набор пакетов. Для Claude-провайдеров этот
# уровень маппится на сонетовскую модель (см. claude_sdk_client.model_for_complexity).
_LLM_COMPLEXITY = "standard"


@dataclass(frozen=True)
class DomainPackSelectionResult:
    provider: str
    model: str
    selected_pack_refs: tuple[str, ...]
    rationale: str
    confidence: float


class DomainPackSelectionService:
    def __init__(self, *, llm_registry: LLMProviderRegistry | None = None) -> None:
        self._llm = llm_registry or LLMProviderRegistry()

    def select_for_request(
        self,
        snapshot: RegistrySnapshot,
        *,
        objective_ref: str,
        request_text: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> DomainPackSelectionResult:
        ObjectRef.parse(objective_ref)
        candidate_packs = self._candidate_packs(snapshot)

        # Stub — не LLM-вызов, идёт особняком (regex matcher).
        # env-переменные больше НЕ управляют выбором провайдера для
        # selector — настройки приходят из settings-store через
        # resolve_for_purpose("domain_pack_selector"). Параметр `provider`
        # остаётся как явный override (тесты, CLI).
        resolved_provider_name = provider or "auto"

        if not candidate_packs:
            return DomainPackSelectionResult(
                provider=resolved_provider_name,
                model=model or "",
                selected_pack_refs=(),
                rationale="В реестре нет активных доменных пакетов.",
                confidence=1.0,
            )

        if resolved_provider_name == "stub":
            return self._select_stub(candidate_packs, request_text, model=model or "stub")

        # LLM-провайдер. Если задан явный provider override (тесты, CLI) —
        # legacy env-based путь. Иначе всегда через settings-store.
        if provider is not None:
            llm = self._llm.get(
                provider=provider,
                model=model,
                complexity=_LLM_COMPLEXITY,
            )
        else:
            llm = self._llm.resolve_for_purpose(
                "domain_pack_selector",
                complexity=_LLM_COMPLEXITY,
                override_model=model,
            )
        return self._select_llm(candidate_packs, request_text, llm=llm)

    def _candidate_packs(
        self,
        snapshot: RegistrySnapshot,
    ) -> tuple[DomainPackSpec, ...]:
        return tuple(
            sorted(
                (pack for pack in snapshot.domain_packs.values() if pack.status == "active"),
                key=lambda item: item.ref.as_string(),
            )
        )

    def _select_stub(
        self,
        candidate_packs: tuple[DomainPackSpec, ...],
        request_text: str,
        *,
        model: str,
    ) -> DomainPackSelectionResult:
        request_lower = request_text.lower()
        request_stems = self._stem_set(request_text)
        selected: list[str] = []
        rationale_parts: list[str] = []
        for pack in candidate_packs:
            matched_signals = [
                signal
                for signal in pack.entry_signals
                if self._signal_matches(request_lower, request_stems, signal)
            ]
            if matched_signals:
                selected.append(pack.ref.as_string())
                rationale_parts.append(
                    f"{pack.ref.as_string()}: совпали сигналы {', '.join(sorted(matched_signals))}"
                )
        if not selected:
            rationale = "Автоматический модуль подбора не нашёл явных сигналов для подключения доменных пакетов."
            confidence = 0.55
        else:
            rationale = (
                "Автоматический модуль подбора выбрал доменные пакеты по сигналам "
                "исходного запроса: " + "; ".join(rationale_parts)
            )
            confidence = 0.78
        return DomainPackSelectionResult(
            provider="stub",
            model=model,
            selected_pack_refs=tuple(sorted(set(selected))),
            rationale=rationale,
            confidence=confidence,
        )

    def _signal_matches(self, request_lower: str, request_stems: set[str], signal: str) -> bool:
        normalized_signal = signal.strip().lower()
        if not normalized_signal:
            return False
        if normalized_signal in request_lower:
            return True
        signal_stems = self._stem_set(normalized_signal)
        return bool(signal_stems) and signal_stems.issubset(request_stems)

    def _stem_set(self, text: str) -> set[str]:
        tokens = re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ]+", text.lower().replace("ё", "е"))
        stems: set[str] = set()
        for token in tokens:
            if len(token) <= 4:
                stems.add(token)
            else:
                stems.add(token[:6])
        return stems

    def _select_llm(
        self,
        candidate_packs: tuple[DomainPackSpec, ...],
        request_text: str,
        *,
        llm,  # LLMProvider — Protocol, не указываем явно, чтобы избежать циклов
    ) -> DomainPackSelectionResult:
        schema: dict[str, object] = {
            "type": "object",
            "required": ["selected_pack_refs", "rationale", "confidence"],
            "additionalProperties": False,
            "properties": {
                "selected_pack_refs": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "confidence": {"type": "number"},
            },
        }
        valid_refs = {pack.ref.as_string() for pack in candidate_packs}
        candidate_lines = [
            "\n".join(
                [
                    f"- ref: {pack.ref.as_string()}",
                    f"  name: {pack.title}",
                    f"  domain: {pack.domain}",
                    f"  description: {pack.description}",
                    f"  entry_signals: {', '.join(pack.entry_signals) if pack.entry_signals else 'нет'}",
                ]
            )
            for pack in candidate_packs
        ]
        system_prompt = (
            "Ты определяешь, какие доменные пакеты нужно включить для обработки бизнес-запроса. "
            "Выбирай минимальный, но достаточный набор пакетов. "
            "Не подключай пакет без реальной необходимости. "
            "Ориентируйся на сам запрос, а не на желание включить всё подряд. "
            "Если пакет не нужен, не выбирай его. "
            "Верни только валидный JSON."
        )
        user_prompt = "\n\n".join(
            [
                "Исходный бизнес-запрос:",
                request_text.strip(),
                "Доступные доменные пакеты:",
                *candidate_lines,
                "Выбери только те пакеты, которые действительно нужны, "
                "чтобы правильно разобрать такой запрос и собрать качественное ТЗ.",
            ]
        )
        payload = llm.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
        raw_selected = payload.get("selected_pack_refs", [])
        if not isinstance(raw_selected, list):
            raise ConflictError("LLM-модуль подбора вернул невалидное поле selected_pack_refs.")
        selected = tuple(sorted({str(item) for item in raw_selected if str(item) in valid_refs}))
        rationale = payload.get("rationale")
        confidence = payload.get("confidence")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ConflictError("LLM-модуль подбора не вернул обоснование выбора.")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ConflictError("LLM-модуль подбора не вернул числовую уверенность.")
        return DomainPackSelectionResult(
            provider=llm.name,
            model=llm.model or "",
            selected_pack_refs=selected,
            rationale=rationale.strip(),
            confidence=float(confidence),
        )


def _env(name: str) -> str | None:
    """Безопасное чтение env: пустая строка трактуется как «не задано»."""
    import os
    value = os.environ.get(name)
    return value if value and value.strip() else None
