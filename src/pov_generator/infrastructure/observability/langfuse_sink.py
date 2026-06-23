"""Опциональный экспорт LLM-вызовов в self-hosted Langfuse (v3.11).

«Ships dark»: по умолчанию — ``NullSink`` (полный no-op). Реальный синк
поднимается ТОЛЬКО когда заданы env-переменные
``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` (и опционально ``LANGFUSE_HOST``
для self-hosted), И установлен пакет ``langfuse``. Любой сбой инициализации или
эмиссии деградирует в no-op — наблюдаемость НИКОГДА не валит LLM-поток.

Почему env, а не зашифрованный settings-store: креды провайдеров (settings.db)
привязаны к LLM-подключению проекта; Langfuse — инфраструктурный/деплой-конфиг
рядом с ``POV_SECRET_KEY``/``POV_LOG_*`` (env-first, как ``secret_box``). Это
также держит промпты в своём периметре — никакого SaaS-эгресса по умолчанию.

**Что кладём в спан** (помимо prompt/response/usage):
- ``session_id`` — группирует все вызовы одного проекта в Langfuse-сессию
  (инженер видит всю цепочку прогона как единый поток); берётся из
  ``context["session_id"]`` или, по умолчанию, из ``project_id``;
- ``tags`` — ``purpose`` + ``provider`` (+ ``error``) для фильтрации в UI;
- богатая ``metadata`` — детализация токенов (cache/reasoning/retry/call_count/
  source/cost), длительность, ключи ответа и весь bound-контекст
  (``project_id``/``task_id``/``artifact_role``/``complexity``…).

**Совместимость SDK.** Распознаём API на этапе сборки клиента: v3 (OTEL,
``start_generation`` + ``update_trace``) или v2 (``trace`` + ``generation``).
Нераспознанный SDK → громкий ``warning`` и ``NullSink`` (а не тихий no-op).

Единственная точка вызова — :func:`record_llm_observation` из
``LoggingLLMProvider`` (чокпоинт всех провайдеров). :func:`flush_llm_observations`
форсирует отправку накопленных спанов — для коротких CLI-прогонов.
"""

from __future__ import annotations

import os
from typing import Any

from ...common.logging import get_logger

logger = get_logger("observability")

# Синглтон: _SENTINEL = ещё не строили; иначе — NullSink/LangfuseSink.
_SENTINEL: Any = object()
_sink_singleton: Any = _SENTINEL


class NullSink:
    """No-op синк — поведение по умолчанию (Langfuse не настроен)."""

    enabled = False

    def emit(self, **_kwargs: Any) -> None:  # noqa: D401 — намеренный no-op
        return

    def flush(self) -> None:  # noqa: D401 — намеренный no-op
        return


class LangfuseSink:
    """Обёртка над клиентом Langfuse: один LLM-вызов → trace (сессия) + generation.

    ``api_version`` фиксируется при сборке (``"v3"`` | ``"v2"``) — emit идёт по
    соответствующей ветке. Любой сбой эмиссии проглатывается (best-effort):
    наблюдаемость не влияет на LLM-поток.
    """

    enabled = True

    def __init__(self, client: Any, *, api_version: str) -> None:
        self._client = client
        self._api = api_version

    def emit(
        self,
        *,
        purpose: str | None = None,
        context: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str = "",
        user_prompt: str = "",
        response: Any = None,
        usage: Any = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        try:
            ctx = dict(context or {})
            name = purpose or "llm_call"
            # Сессия группирует все вызовы прогона; по умолчанию — проект.
            session_id = ctx.get("session_id") or ctx.get("project_id")
            user_id = ctx.get("user_id")
            tags = self._tags(purpose, provider, error)
            metadata = self._metadata(
                purpose=purpose,
                provider=provider,
                model=model,
                duration_ms=duration_ms,
                usage=usage,
                response=response,
                error=error,
                ctx=ctx,
            )
            usage_payload = self._usage_payload(usage)
            input_payload = {"system": system_prompt, "user": user_prompt}
            output_payload = None if error else response
            level = "ERROR" if error else "DEFAULT"
            if self._api == "v3":
                self._emit_v3(
                    name=name,
                    model=model,
                    input=input_payload,
                    output=output_payload,
                    usage=usage_payload,
                    metadata=metadata,
                    level=level,
                    status_message=error,
                    session_id=session_id,
                    user_id=user_id,
                    tags=tags,
                )
            else:
                self._emit_v2(
                    name=name,
                    model=model,
                    input=input_payload,
                    output=output_payload,
                    usage=usage_payload,
                    metadata=metadata,
                    level=level,
                    status_message=error,
                    session_id=session_id,
                    user_id=user_id,
                    tags=tags,
                )
        except Exception:  # noqa: BLE001 — наблюдаемость не валит поток
            return

    # -- ветки SDK ----------------------------------------------------------

    def _emit_v2(
        self,
        *,
        name: str,
        model: str | None,
        input: dict[str, Any],
        output: Any,
        usage: dict[str, Any] | None,
        metadata: dict[str, Any],
        level: str,
        status_message: str | None,
        session_id: str | None,
        user_id: str | None,
        tags: list[str],
    ) -> None:
        # Trace несёт сессию/теги/контекст; generation — полный prompt/response.
        trace = self._client.trace(
            name=name,
            session_id=session_id,
            user_id=user_id,
            tags=tags or None,
            metadata=metadata,
        )
        gen_factory = getattr(trace, "generation", None) or self._client.generation
        gen_factory(
            name=name,
            model=model,
            input=input,
            output=output,
            usage=usage,
            metadata=metadata,
            level=level,
            status_message=status_message,
        )

    def _emit_v3(
        self,
        *,
        name: str,
        model: str | None,
        input: dict[str, Any],
        output: Any,
        usage: dict[str, Any] | None,
        metadata: dict[str, Any],
        level: str,
        status_message: str | None,
        session_id: str | None,
        user_id: str | None,
        tags: list[str],
    ) -> None:
        gen = self._client.start_generation(
            name=name,
            model=model,
            input=input,
            metadata=metadata,
        )
        try:
            gen.update(
                output=output,
                usage_details=usage,
                level=level,
                status_message=status_message,
            )
            update_trace = getattr(gen, "update_trace", None)
            if update_trace is not None:
                update_trace(
                    session_id=session_id,
                    user_id=user_id,
                    tags=tags or None,
                )
        finally:
            gen.end()

    def flush(self) -> None:
        """Форсировать отправку накопленных спанов (для коротких CLI-прогонов)."""
        try:
            flush = getattr(self._client, "flush", None)
            if flush is not None:
                flush()
        except Exception:  # noqa: BLE001 — best-effort
            return

    # -- сборка полезной нагрузки ------------------------------------------

    @staticmethod
    def _usage_payload(usage: Any) -> dict[str, Any] | None:
        if usage is None:
            return None
        return {
            "input": getattr(usage, "input_tokens", None),
            "output": getattr(usage, "output_tokens", None),
            "total": getattr(usage, "total_tokens", None),
        }

    @staticmethod
    def _tags(purpose: str | None, provider: str | None, error: str | None) -> list[str]:
        tags = [str(t) for t in (purpose, provider) if t]
        if error:
            tags.append("error")
        return tags

    @staticmethod
    def _metadata(
        *,
        purpose: str | None,
        provider: str | None,
        model: str | None,
        duration_ms: int | None,
        usage: Any,
        response: Any,
        error: str | None,
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "duration_ms": duration_ms,
        }
        if usage is not None:
            meta.update(
                {
                    "tokens_input": getattr(usage, "input_tokens", None),
                    "tokens_output": getattr(usage, "output_tokens", None),
                    "tokens_total": getattr(usage, "total_tokens", None),
                    "tokens_cache_read": getattr(usage, "cache_tokens", None),
                    "tokens_reasoning": getattr(usage, "reasoning_tokens", None),
                    "call_count": getattr(usage, "call_count", None),
                    "retry_count": getattr(usage, "retry_count", None),
                    "usage_source": getattr(usage, "source", None),
                    "cost_usd": getattr(usage, "cost_usd", None),
                }
            )
        if isinstance(response, dict):
            meta["response_keys"] = sorted(str(k) for k in response.keys())
        if error:
            meta["error"] = error
        # Bound-контекст вызова (project_id/task_id/artifact_role/complexity…);
        # session_id/user_id выносятся в trace-поля, в metadata их не дублируем.
        for key, value in ctx.items():
            if key in ("session_id", "user_id") or value is None:
                continue
            meta.setdefault(key, value)
        return {k: v for k, v in meta.items() if v is not None}


def _detect_api(client: Any) -> str | None:
    """Распознать поколение SDK по доступным методам.

    v3 (OTEL) — ``start_generation``; v2 — ``trace``/``generation``. Ничего из
    этого нет → ``None`` (вызывающий поднимет warning и деградирует в NullSink).
    """
    if hasattr(client, "start_generation"):
        return "v3"
    if hasattr(client, "trace") and hasattr(client, "generation"):
        return "v2"
    return None


def _build_sink() -> Any:
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    if not (public and secret):
        return NullSink()
    try:
        from langfuse import Langfuse  # type: ignore
    except Exception:  # noqa: BLE001 — пакет не установлен → no-op
        logger.info("Langfuse env задан, но пакет не установлен — трассировка отключена")
        return NullSink()
    try:
        client = Langfuse(public_key=public, secret_key=secret, host=host or None)
    except Exception as exc:  # noqa: BLE001 — кривой конфиг → no-op
        logger.warning("не удалось поднять клиент Langfuse — трассировка отключена", error=str(exc))
        return NullSink()
    api_version = _detect_api(client)
    if api_version is None:
        # Громко, а не тихо: SDK есть, но его API не распознан (слишком старый/новый).
        logger.warning(
            "Langfuse SDK с нераспознанным API (нет ни start_generation, ни "
            "trace/generation) — трассировка отключена; обновите пакет langfuse",
            sdk_version=_langfuse_version(),
        )
        return NullSink()
    logger.info("Langfuse трассировка включена", host=host or "default", api=api_version)
    return LangfuseSink(client, api_version=api_version)


def _langfuse_version() -> str:
    try:
        import langfuse  # type: ignore

        return str(getattr(langfuse, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def get_llm_sink() -> Any:
    """Ленивый синглтон синка (NullSink, пока env не настроен)."""
    global _sink_singleton
    if _sink_singleton is _SENTINEL:
        _sink_singleton = _build_sink()
    return _sink_singleton


def set_llm_sink(sink: Any) -> None:
    """Тест-хук: подменить синк (например, фейковым, фиксирующим вызовы)."""
    global _sink_singleton
    _sink_singleton = sink


def reset_llm_sink() -> None:
    """Тест-хук: сбросить кэш синглтона (перечитает env при следующем get)."""
    global _sink_singleton
    _sink_singleton = _SENTINEL


def record_llm_observation(**kwargs: Any) -> None:
    """Эмитнуть наблюдение в активный синк. Полностью best-effort: любой сбой
    проглатывается — наблюдаемость не должна влиять на LLM-поток."""
    try:
        get_llm_sink().emit(**kwargs)
    except Exception:  # noqa: BLE001
        return


def flush_llm_observations() -> None:
    """Форсировать отправку накопленных спанов в Langfuse (best-effort).

    Полезно в конце короткого CLI-прогона: фоновый поток SDK мог не успеть
    отправить батч до выхода интерпретатора. No-op для NullSink.
    """
    try:
        get_llm_sink().flush()
    except Exception:  # noqa: BLE001
        return
