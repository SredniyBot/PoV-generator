"""Тесты единой capability-матрицы окружения (правила R1–R5, M)."""
from __future__ import annotations

from pov_generator.domain.environment_compatibility import (
    agent_environment_readiness,
    compatible_provider_types,
    model_belongs_to,
    needs_llm_connection,
    relevant_fields,
    valid_engines,
)


# R1 — адаптер × движок
def test_valid_engines() -> None:
    assert valid_engines("claude_code") == ("docker", "host")
    assert valid_engines("aider") == ("docker",)
    assert valid_engines("command") == ("docker",)
    assert valid_engines("stub") == ()


# R2/R3 — совместимые типы LLM-провайдера
def test_compatible_provider_types() -> None:
    # host (claude_code) — сессия, отдельное подключение не требуется.
    assert compatible_provider_types("claude_code", "host") == ()
    # docker claude_code — нужен Anthropic-ключ (openrouter не подойдёт).
    assert compatible_provider_types("claude_code", "docker") == ("anthropic",)
    # aider — openrouter|anthropic, claude_cli исключён (нет ключа).
    assert set(compatible_provider_types("aider", "docker")) == {"openrouter", "anthropic"}
    assert "claude_cli" not in compatible_provider_types("aider", "docker")
    # stub — LLM не нужен.
    assert compatible_provider_types("stub", "docker") == ()


def test_needs_llm_connection() -> None:
    assert needs_llm_connection("aider", "docker") is True
    assert needs_llm_connection("claude_code", "host") is False  # сессия
    assert needs_llm_connection("stub", "docker") is False


# R4 — релевантность полей
def test_relevant_fields() -> None:
    assert relevant_fields("stub", "docker") == frozenset()
    assert "image" not in relevant_fields("claude_code", "host")  # на хосте образ не нужен
    assert "host_security" in relevant_fields("claude_code", "host")
    docker = relevant_fields("aider", "docker")
    assert {"image", "network", "model"} <= docker
    assert "command" in relevant_fields("command", "docker")


# M — семья модели
def test_model_belongs_to() -> None:
    assert model_belongs_to("openai/gpt-4.1-mini", "openrouter") is True
    assert model_belongs_to("openai/gpt-4.1-mini", "claude_cli") is False  # openrouter ≠ claude
    assert model_belongs_to("claude-sonnet-4-5", "anthropic") is True
    assert model_belongs_to("claude-sonnet-4-5", "claude_cli") is True
    assert model_belongs_to("claude-sonnet-4-5", "openrouter") is False
    # Неизвестную семью не блокируем (кастом/свободный ввод).
    assert model_belongs_to("my-local-model", "openrouter") is True


# Готовность окружения (для графа)
def test_readiness_stub_always_ok() -> None:
    assert agent_environment_readiness(
        adapter="stub", engine="docker", configured_provider_types=frozenset()
    ).ok


def test_readiness_host_ok_without_llm() -> None:
    r = agent_environment_readiness(
        adapter="claude_code", engine="host", configured_provider_types=frozenset()
    )
    assert r.ok  # host использует сессию, LLM-подключение не нужно


def test_readiness_docker_needs_compatible_provider() -> None:
    # aider в docker без совместимого провайдера (есть только claude_cli) → не готово.
    blocked = agent_environment_readiness(
        adapter="aider", engine="docker", configured_provider_types=frozenset({"claude_cli"})
    )
    assert not blocked.ok
    assert "openrouter" in blocked.reason and "anthropic" in blocked.reason
    # с openrouter — готово.
    ok = agent_environment_readiness(
        adapter="aider", engine="docker", configured_provider_types=frozenset({"openrouter"})
    )
    assert ok.ok


def test_readiness_invalid_engine() -> None:
    r = agent_environment_readiness(
        adapter="aider", engine="host", configured_provider_types=frozenset({"openrouter"})
    )
    assert not r.ok
