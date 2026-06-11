"""Совместимость окружения исполнения — ЕДИНЫЙ источник правды.

Две ортогональные оси исполнения:

* **LLM-провайдер** (``openrouter`` | ``anthropic`` | ``claude_cli``) — выполняет
  рассуждение/документы и даёт harness-агенту модель и креды.
* **Harness-исполнитель** — адаптер (``stub`` | ``claude_code`` | ``aider`` |
  ``command``) × движок (``docker`` | ``host``) — выполняет агентские задачи кода.

Не все связки допустимы. Правила (R1–R5, M) собраны ЗДЕСЬ, и из них выводятся:
опции и валидация настроек окружения, и сигнал «запустится ли агентская задача»
на графе. Чистый домен: без I/O — на вход подаются уже загруженные настройки.

Правила:
  R1  engine=host ⇒ adapter=claude_code (только он переиспользует сессию claude CLI).
  R2  claude_cli не отдаёт API-ключ в песочницу ⇒ docker-агент не может его
      использовать; claude_cli пригоден только для claude_code на host.
  R3  aider требует API-key провайдер (openrouter|anthropic).
  R4  релевантность полей: image/network — только docker; host_security — только
      host; model-override — из моделей совместимого провайдера.
  R5  model-override (если задан) обязан принадлежать совместимому провайдеру.
  M   модель принадлежит ровно одной семье провайдеров (openrouter vs Claude).
"""

from __future__ import annotations

from dataclasses import dataclass

# Оси (зеркало доменных Literal'ов harness_settings / llm_settings).
HARNESS_ADAPTERS: tuple[str, ...] = ("stub", "claude_code", "aider", "command")
HARNESS_ENGINES: tuple[str, ...] = ("docker", "host")
LLM_PROVIDER_TYPES: tuple[str, ...] = ("openrouter", "anthropic", "claude_cli")


# --- ось harness: адаптер × движок ----------------------------------------

def valid_engines(adapter: str) -> tuple[str, ...]:
    """Допустимые движки для адаптера (R1)."""
    if adapter == "claude_code":
        return ("docker", "host")
    if adapter in ("aider", "command"):
        return ("docker",)
    return ()  # stub — движок не применим


def supports_host(adapter: str) -> bool:
    return "host" in valid_engines(adapter)


def needs_llm_connection(adapter: str, engine: str) -> bool:
    """Нужен ли настроенный LLM-провайдер (модель+ключ из настроек).

    ``stub`` — нет (фикстуры). ``host`` — нет (claude_code берёт залогиненную
    сессию claude CLI, без ключей). ``docker`` non-stub — да (ключ инжектится
    в песочницу).
    """
    if adapter == "stub":
        return False
    if engine == "host":
        return False
    return True


def compatible_provider_types(adapter: str, engine: str) -> tuple[str, ...]:
    """Типы LLM-провайдера, способные дать креды для пары адаптер+движок (R2,R3).

    Пусто, если LLM-подключение не требуется (stub / host). Для docker:
      * claude_code — нужен Anthropic-ключ (openrouter-ключ Claude Code не примет);
      * aider/command — openrouter или anthropic (litellm); claude_cli исключён
        (нет API-ключа для песочницы).
    """
    if not needs_llm_connection(adapter, engine):
        return ()
    if adapter == "claude_code":
        return ("anthropic",)
    if adapter in ("aider", "command"):
        return ("openrouter", "anthropic")
    return ()


def relevant_fields(adapter: str, engine: str) -> frozenset[str]:
    """Какие поля конфигурации ОСМЫСЛЕННЫ для пары адаптер+движок (R4).

    Поля вне набора UI не показывает и не сохраняет (image для host бессмыслен и
    т.п.). Возможные поля: ``image``, ``network``, ``host_security``, ``model``,
    ``command``.
    """
    if adapter == "stub":
        return frozenset()
    if engine == "host":  # только claude_code на host
        return frozenset({"host_security", "model"})
    fields = {"image", "network", "model"}
    if adapter == "command":
        fields.add("command")
    return frozenset(fields)


# --- ось LLM: модель × семья провайдера (M) ---------------------------------

def model_provider_family(model_name: str) -> str | None:
    """Семья провайдера по имени модели или ``None`` (не определить).

    Эвристика по namespace: ``vendor/model`` (есть «/») → openrouter; имя на
    ``claude…`` → семья Claude (anthropic/claude_cli). Иначе — неизвестно
    (не блокируем свободный ввод)."""
    name = (model_name or "").strip().lower()
    if not name:
        return None
    if "/" in name:
        return "openrouter"
    if name.startswith("claude"):
        return "claude"
    return None


def model_belongs_to(model_name: str, provider_type: str) -> bool:
    """Может ли модель принадлежать провайдеру данного типа (M).

    Неизвестную семью (None) не блокируем — это свободный ввод/кастом."""
    family = model_provider_family(model_name)
    if family is None:
        return True
    if family == "openrouter":
        return provider_type == "openrouter"
    if family == "claude":
        return provider_type in ("anthropic", "claude_cli")
    return True


# --- готовность окружения (для графа и валидации настроек) -------------------

@dataclass(frozen=True)
class AgentEnvReadiness:
    """Готово ли окружение запускать агентскую (harness) задачу.

    ``ok=False`` ⇒ ``reason`` — короткая причина для бейджа на узле графа.
    Уровень — КОНФИГУРАЦИЯ (совместимость + настроен ли провайдер), без живого
    зонда Docker/образа (это состояние среды на странице «Настройки окружения»).
    """

    ok: bool
    reason: str = ""


def agent_environment_readiness(
    *,
    adapter: str,
    engine: str,
    configured_provider_types: frozenset[str],
) -> AgentEnvReadiness:
    """Конфигурационная готовность агентского окружения.

    ``configured_provider_types`` — типы LLM-провайдеров, для которых есть
    рабочее подключение в настройках.
    """
    if adapter == "stub":
        return AgentEnvReadiness(ok=True)
    if engine not in valid_engines(adapter):
        return AgentEnvReadiness(
            ok=False,
            reason=f"Движок «{engine}» несовместим с адаптером «{adapter}».",
        )
    if engine == "host":
        # claude_code на host использует залогиненную сессию claude CLI — ключи
        # из настроек не нужны (саму сессию здесь не проверяем).
        return AgentEnvReadiness(ok=True)
    compatible = compatible_provider_types(adapter, engine)
    if compatible and not (configured_provider_types & set(compatible)):
        human = " или ".join(compatible)
        return AgentEnvReadiness(
            ok=False,
            reason=(
                f"Агенту «{adapter}» нужен LLM-провайдер: {human}. "
                "Настройте его в «Настройки → LLM»."
            ),
        )
    return AgentEnvReadiness(ok=True)
