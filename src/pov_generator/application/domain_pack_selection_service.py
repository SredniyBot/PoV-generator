from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..common.errors import ConflictError
from ..domain.registry import DomainPackSpec, ObjectRef, RegistrySnapshot
from ..infrastructure.claude_sdk_client import (
    ClaudeSdkClient,
    model_for_complexity as claude_sdk_model_for_complexity,
)
from ..infrastructure.claude_subscription_client import (
    ClaudeSubscriptionClient,
    model_for_complexity as claude_subscription_model_for_complexity,
)
from ..infrastructure.openrouter_client import OpenRouterClient, OpenRouterConfig

# Селектор domain pack'ов и task-runner вызывают LLM из «standard»-зоны
# сложности: задача не тривиальная (нужно понять запрос), но и не на грани
# сложности артефакта-синтеза. Маппинг complexity → модель для Claude
# живёт в инфраструктурных клиентах; здесь просто фиксируем уровень.
_LLM_COMPLEXITY = "standard"


@dataclass(frozen=True)
class DomainPackSelectionResult:
    provider: str
    model: str
    selected_pack_refs: tuple[str, ...]
    rationale: str
    confidence: float


class DomainPackSelectionService:
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
        active_provider = provider or os.environ.get("POV_DOMAIN_PACK_SELECTION_PROVIDER")
        if not active_provider:
            # Авто-выбор провайдера, если в env не задано явно. Подхватываем
            # тот же провайдер, что и для основного workflow
            # (POV_EXECUTION_PROVIDER), иначе fallback по наличию ключа.
            active_provider = (
                os.environ.get("POV_EXECUTION_PROVIDER")
                or ("openrouter" if os.environ.get("POV_OPENROUTER_API_KEY") else "stub")
            )
        active_model = (
            model
            or os.environ.get("POV_DOMAIN_PACK_SELECTION_MODEL")
            or self._default_model_for_provider(active_provider)
        )

        if not candidate_packs:
            return DomainPackSelectionResult(
                provider=active_provider,
                model=active_model,
                selected_pack_refs=(),
                rationale="В реестре нет активных доменных пакетов.",
                confidence=1.0,
            )

        if active_provider == "stub":
            return self._select_stub(candidate_packs, request_text, model=active_model)
        if active_provider in ("openrouter", "claude_sdk", "claude_subscription"):
            return self._select_llm(
                candidate_packs,
                request_text,
                provider=active_provider,
                model=active_model,
            )
        raise ConflictError(
            f"Неподдерживаемый provider выбора доменных пакетов: {active_provider}. "
            "Поддерживаются: stub, openrouter, claude_sdk, claude_subscription."
        )

    def _default_model_for_provider(self, provider: str) -> str:
        """Дефолтная модель, если ни флаг команды, ни env не заданы.

        Для claude-провайдеров возвращаем рекомендованную для «standard»
        задач модель (CLI subscription может вернуть None — тогда берём
        пустую строку: модель определит сам CLI/Anthropic API)."""
        if provider == "openrouter":
            return os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        if provider == "claude_sdk":
            return claude_sdk_model_for_complexity(_LLM_COMPLEXITY)
        if provider == "claude_subscription":
            return claude_subscription_model_for_complexity(_LLM_COMPLEXITY) or ""
        return ""

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
            rationale = "Автоматический модуль подбора выбрал доменные пакеты по сигналам исходного запроса: " + "; ".join(rationale_parts)
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
        provider: str,
        model: str,
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
        candidate_lines = []
        valid_refs = {pack.ref.as_string() for pack in candidate_packs}
        for pack in candidate_packs:
            candidate_lines.append(
                "\n".join(
                    [
                        f"- ref: {pack.ref.as_string()}",
                f"  name: {pack.title}",
                        f"  domain: {pack.domain}",
                        f"  description: {pack.description}",
                        f"  entry_signals: {', '.join(pack.entry_signals) if pack.entry_signals else 'нет'}",
                    ]
                )
            )
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
                "Выбери только те пакеты, которые действительно нужны, чтобы правильно разобрать такой запрос и собрать качественное ТЗ.",
            ]
        )
        payload = self._chat_json(
            provider=provider,
            model=model,
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
            provider=provider,
            model=model,
            selected_pack_refs=selected,
            rationale=rationale.strip(),
            confidence=float(confidence),
        )

    def _chat_json(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
    ) -> dict:
        """Единая точка вызова LLM. Тот же контракт, что у
        `ExecutionService._chat_json_via_provider`: возвращает dict,
        соответствующий схеме."""
        if provider == "openrouter":
            return self._openrouter_client(model).chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        if provider == "claude_sdk":
            return ClaudeSdkClient.from_env(model=model).chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        if provider == "claude_subscription":
            return ClaudeSubscriptionClient.from_env(model=model or None).chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        raise ConflictError(f"Неподдерживаемый provider: {provider}")

    def _openrouter_client(self, model: str) -> OpenRouterClient:
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY.")
        base_url = os.environ.get("POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        return OpenRouterClient(
            OpenRouterConfig(
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
        )
