"""Тесты для :mod:`infrastructure.observability.langfuse_sink`.

Фиксируют контракт обогащённого спана: сессия (group-by project), теги,
богатая metadata (детализация токенов + bound-контекст), совместимость v2/v3
SDK и громкую деградацию при нераспознанном API. Реальный пакет ``langfuse``
не нужен — подменяем фейковыми клиентами.
"""

from __future__ import annotations

from typing import Any

from pov_generator.infrastructure.llm.protocol import LLMUsage
from pov_generator.infrastructure.observability import langfuse_sink as sink_mod
from pov_generator.infrastructure.observability.langfuse_sink import (
    LangfuseSink,
    NullSink,
    _build_sink,
    _detect_api,
)

# ---------------------------------------------------------------------------
# Фейковые клиенты SDK
# ---------------------------------------------------------------------------


class _FakeGeneration:
    """v2: дочерний generation, созданный из trace."""

    def __init__(self, sink: "_FakeV2Client", **kwargs: Any) -> None:
        self._sink = sink
        sink.generations.append(kwargs)


class _FakeTrace:
    def __init__(self, sink: "_FakeV2Client", **kwargs: Any) -> None:
        self._sink = sink
        sink.traces.append(kwargs)

    def generation(self, **kwargs: Any) -> _FakeGeneration:
        return _FakeGeneration(self._sink, **kwargs)


class _FakeV2Client:
    def __init__(self) -> None:
        self.traces: list[dict] = []
        self.generations: list[dict] = []
        self.flushed = 0

    def trace(self, **kwargs: Any) -> _FakeTrace:
        return _FakeTrace(self, **kwargs)

    def generation(self, **kwargs: Any) -> _FakeGeneration:  # top-level fallback
        return _FakeGeneration(self, **kwargs)

    def flush(self) -> None:
        self.flushed += 1


class _FakeV3Span:
    def __init__(self, sink: "_FakeV3Client", **kwargs: Any) -> None:
        self._sink = sink
        sink.started.append(kwargs)
        self.updates: list[dict] = []
        self.trace_updates: list[dict] = []
        self.ended = False
        sink.spans.append(self)

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def update_trace(self, **kwargs: Any) -> None:
        self.trace_updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class _FakeV3Client:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.spans: list[_FakeV3Span] = []
        self.flushed = 0

    def start_generation(self, **kwargs: Any) -> _FakeV3Span:
        return _FakeV3Span(self, **kwargs)

    def flush(self) -> None:
        self.flushed += 1


def _usage() -> LLMUsage:
    return LLMUsage(
        input_tokens=120,
        output_tokens=40,
        total_tokens=160,
        source="actual",
        cache_tokens=12,
        reasoning_tokens=8,
        call_count=2,
        retry_count=1,
    )


# ---------------------------------------------------------------------------
# Распознавание API
# ---------------------------------------------------------------------------


def test_detect_api_prefers_v3() -> None:
    assert _detect_api(_FakeV3Client()) == "v3"


def test_detect_api_v2() -> None:
    assert _detect_api(_FakeV2Client()) == "v2"


def test_detect_api_unknown() -> None:
    class _Bare:
        pass

    assert _detect_api(_Bare()) is None


def test_build_sink_unknown_api_degrades_to_nullsink(monkeypatch) -> None:
    """SDK есть, но API не распознан → NullSink (а не падение/тихий no-op)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    class _Bare:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    import types

    fake_module = types.SimpleNamespace(Langfuse=_Bare, __version__="9.9.9")
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake_module)
    assert isinstance(_build_sink(), NullSink)


def test_build_sink_nullsink_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert isinstance(_build_sink(), NullSink)


# ---------------------------------------------------------------------------
# v2-эмиссия: trace (сессия/теги) + generation (prompt/response/usage)
# ---------------------------------------------------------------------------


def test_v2_emit_groups_by_project_session_and_tags() -> None:
    client = _FakeV2Client()
    sink = LangfuseSink(client, api_version="v2")
    sink.emit(
        purpose="decision_identification",
        context={"project_id": "p-1", "task_id": "t-9", "complexity": "complex"},
        provider="openrouter",
        model="openai/gpt-4.1",
        system_prompt="SYS",
        user_prompt="USR",
        response={"decisions": [], "summary": "x"},
        usage=_usage(),
        duration_ms=1234,
    )

    assert len(client.traces) == 1
    trace = client.traces[0]
    # Сессия = project_id (группировка прогона), теги = purpose + provider.
    assert trace["session_id"] == "p-1"
    assert trace["tags"] == ["decision_identification", "openrouter"]
    assert trace["name"] == "decision_identification"

    assert len(client.generations) == 1
    gen = client.generations[0]
    assert gen["model"] == "openai/gpt-4.1"
    assert gen["input"] == {"system": "SYS", "user": "USR"}
    assert gen["output"] == {"decisions": [], "summary": "x"}
    assert gen["usage"] == {"input": 120, "output": 40, "total": 160}
    assert gen["level"] == "DEFAULT"

    meta = gen["metadata"]
    # Детализация токенов.
    assert meta["tokens_cache_read"] == 12
    assert meta["tokens_reasoning"] == 8
    assert meta["call_count"] == 2
    assert meta["retry_count"] == 1
    assert meta["usage_source"] == "actual"
    assert meta["duration_ms"] == 1234
    # Bound-контекст просочился в metadata, но session_id туда не дублируется.
    assert meta["task_id"] == "t-9"
    assert meta["complexity"] == "complex"
    assert "session_id" not in meta
    # Фингерпринт ответа — ключи верхнего уровня.
    assert meta["response_keys"] == ["decisions", "summary"]


def test_v2_emit_error_sets_level_and_tag() -> None:
    client = _FakeV2Client()
    sink = LangfuseSink(client, api_version="v2")
    sink.emit(
        purpose="primary_generation",
        context={"project_id": "p-1"},
        provider="claude_sdk",
        model="opus",
        system_prompt="s",
        user_prompt="u",
        response=None,
        usage=None,
        duration_ms=10,
        error="boom",
    )
    gen = client.generations[0]
    assert gen["level"] == "ERROR"
    assert gen["status_message"] == "boom"
    assert gen["output"] is None
    assert "error" in client.traces[0]["tags"]


# ---------------------------------------------------------------------------
# v3-эмиссия: start_generation → update(+usage_details) → update_trace → end
# ---------------------------------------------------------------------------


def test_v3_emit_uses_start_generation_and_update_trace() -> None:
    client = _FakeV3Client()
    sink = LangfuseSink(client, api_version="v3")
    sink.emit(
        purpose="primary_generation",
        context={"project_id": "p-2", "task_id": "t-1", "artifact_role": "spec"},
        provider="claude_subscription",
        model=None,
        system_prompt="S",
        user_prompt="U",
        response={"ok": True},
        usage=_usage(),
        duration_ms=77,
    )
    assert len(client.spans) == 1
    span = client.spans[0]
    assert span.ended is True
    started = client.started[0]
    assert started["name"] == "primary_generation"
    assert started["input"] == {"system": "S", "user": "U"}
    assert started["metadata"]["artifact_role"] == "spec"

    update = span.updates[0]
    assert update["output"] == {"ok": True}
    assert update["usage_details"] == {"input": 120, "output": 40, "total": 160}

    trace_update = span.trace_updates[0]
    assert trace_update["session_id"] == "p-2"
    assert trace_update["tags"] == ["primary_generation", "claude_subscription"]


# ---------------------------------------------------------------------------
# Best-effort: сбой клиента не пробрасывается; flush; NullSink.flush no-op
# ---------------------------------------------------------------------------


def test_emit_swallows_client_errors() -> None:
    class _Boom:
        def trace(self, **_kwargs: Any):
            raise RuntimeError("network down")

        def generation(self, **_kwargs: Any):
            raise RuntimeError("network down")

    sink = LangfuseSink(_Boom(), api_version="v2")
    # Не должно бросить.
    sink.emit(purpose="x", context={"project_id": "p"}, provider="pr", model="m")


def test_flush_delegates_to_client() -> None:
    client = _FakeV2Client()
    sink = LangfuseSink(client, api_version="v2")
    sink.flush()
    assert client.flushed == 1


def test_nullsink_flush_is_noop() -> None:
    NullSink().flush()  # не должно бросить


def test_flush_llm_observations_uses_active_sink() -> None:
    client = _FakeV3Client()
    sink_mod.set_llm_sink(LangfuseSink(client, api_version="v3"))
    try:
        sink_mod.flush_llm_observations()
    finally:
        sink_mod.reset_llm_sink()
    assert client.flushed == 1
