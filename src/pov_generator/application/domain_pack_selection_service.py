from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from ..common.errors import ConflictError
from ..domain.registry import DomainPackSpec, ObjectRef, RegistrySnapshot
from ..infrastructure.openrouter_client import OpenRouterClient, OpenRouterConfig


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
        recipe_ref: str,
        request_text: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> DomainPackSelectionResult:
        recipe_object_ref = ObjectRef.parse(recipe_ref)
        candidate_packs = self._candidate_packs(snapshot, recipe_object_ref)
        active_provider = provider or os.environ.get("POV_DOMAIN_PACK_SELECTION_PROVIDER")
        if not active_provider:
            active_provider = "openrouter" if os.environ.get("POV_OPENROUTER_API_KEY") else "stub"
        active_model = (
            model
            or os.environ.get("POV_DOMAIN_PACK_SELECTION_MODEL")
            or os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        )

        if not candidate_packs:
            return DomainPackSelectionResult(
                provider=active_provider,
                model=active_model,
                selected_pack_refs=(),
                rationale="Для выбранного recipe нет совместимых domain pack.",
                confidence=1.0,
            )

        if active_provider == "stub":
            return self._select_stub(candidate_packs, request_text, model=active_model)
        if active_provider == "openrouter":
            return self._select_openrouter(candidate_packs, request_text, model=active_model)
        raise ConflictError(f"Неподдерживаемый provider выбора domain pack: {active_provider}")

    def _candidate_packs(
        self,
        snapshot: RegistrySnapshot,
        recipe_ref: ObjectRef,
    ) -> tuple[DomainPackSpec, ...]:
        candidates: list[DomainPackSpec] = []
        for pack in snapshot.domain_packs.values():
            if pack.status != "active":
                continue
            for fragment_ref in pack.recipe_fragment_refs:
                fragment = snapshot.resolve_recipe_fragment(fragment_ref)
                if recipe_ref.as_string() in {target.as_string() for target in fragment.target_recipe_refs}:
                    candidates.append(pack)
                    break
        return tuple(sorted(candidates, key=lambda item: item.ref.as_string()))

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

    def _select_openrouter(
        self,
        candidate_packs: tuple[DomainPackSpec, ...],
        request_text: str,
        *,
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
                        f"  name: {pack.name}",
                        f"  domain: {pack.domain}",
                        f"  description: {pack.description}",
                        f"  entry_signals: {', '.join(pack.entry_signals) if pack.entry_signals else 'нет'}",
                    ]
                )
            )
        system_prompt = (
            "Ты определяешь, какие domain pack нужно включить для обработки бизнес-запроса. "
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
                "Доступные domain pack для выбранного recipe:",
                *candidate_lines,
                "Выбери только те пакеты, которые действительно нужны, чтобы правильно разобрать такой запрос и собрать качественное ТЗ.",
            ]
        )
        payload = self._openrouter_client(model).chat_json(
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
            provider="openrouter",
            model=model,
            selected_pack_refs=selected,
            rationale=rationale.strip(),
            confidence=float(confidence),
        )

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
