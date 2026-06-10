"""Клиент Claude через подписку Claude Code.

Использует библиотеку `claude-agent-sdk`, которая запускает локальный CLI
`claude` (он должен быть установлен и залогинен через `claude login`).
Авторизация происходит по сессии CLI, отдельный API-key не нужен.

Важно про выбор CLI: пакет ``claude-agent-sdk`` приходит с **bundled**
бинарником ``_bundled/claude.exe`` и по умолчанию его и запускает. Этот
bundled CLI НЕ авторизован — он не знает про сессию пользователя,
поднятую через ``claude login`` глобально. Если не задать ``cli_path``
явно, SDK уйдёт в bundled, тот висит на старте → SDK выдаёт
"Control request timeout: initialize" через 60 секунд.

Решение: при создании клиента находим системный ``claude`` через
``shutil.which`` (или env-override ``POV_CLAUDE_CLI_PATH``) и
передаём его в ``ClaudeAgentOptions.cli_path``. Если ни системный,
ни override-путь не найдены — fail fast с понятным сообщением.

Structured output: SDK поддерживает нативное соблюдение схемы —
``ClaudeAgentOptions.output_format = {"type": "json_schema", "schema": …}``
транслируется в флаг CLI ``--json-schema``, и готовый payload приходит в
``ResultMessage.structured_output`` (уже распарсенный dict, без хрупкого
извлечения JSON из текста). Схема для CLI очищается от ``description``
(структуре enforcement'а описания не нужны, а аргумент командной строки
ограничен ~32К символов на Windows) — полная схема с описаниями остаётся
в промпте как guidance. Деградация: CLI без поддержки флага / слишком
большая схема / ошибка структурного режима → прежний путь «схема текстом
в промпте + извлечение JSON из ответа» (downstream-слои нормализации и
self-repair гарантируют контракт; см. ``llm/structured_output.py``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.cancellation import CancellationError, CancellationToken, current_cancellation
from ..common.errors import ConflictError
from ..common.logging import get_logger
from .llm.protocol import LLMResult, LLMUsage
from .llm.structured_output import strip_descriptions, strip_nulls

logger = get_logger("llm")

# Бюджет на сериализованную схему в аргументе CLI. Полный лимит командной
# строки Windows — 32767 символов; оставляем запас на путь к CLI, флаги и
# путь к файлу system-prompt.
_CLI_SCHEMA_MAX_CHARS = 24_000

# CLI-бинарники (по пути), которые НЕ знают флаг ``--json-schema``. Узнаётся по
# факту первого отказа «unknown option» и кэшируется НА ВЕСЬ ПРОЦЕСС, но с
# привязкой к конкретному cli_path: версия CLI стабильна в пределах процесса, а
# разные проекты могут указывать разные бинарники (POV_CLAUDE_CLI_PATH), поэтому
# глобальный bool был бы слишком грубым. Запись «только добавление» — гонок,
# меняющих смысл, нет (множество лишь растёт одним и тем же значением).
_FLAG_UNSUPPORTED_CLIS: set[str] = set()


def _is_schema_mode_error(message: str) -> bool:
    """Ошибка относится именно к структурному режиму (флаг/схема), а не к сети?

    Только на таких ошибках мы отключаем enforcement и повторяем без схемы.
    Транзиентные сбои CLI (см. ``_is_transient_cli_error``) сюда НЕ относятся —
    их нельзя путать с «CLI не умеет --json-schema», иначе сетевой сбой
    навсегда лишал бы задачи enforcement'а (разбор инцидента: см. историю).
    """
    low = message.lower()
    return (
        "unknown option" in low
        or "--json-schema" in low
        or "json-schema" in low
        or "invalid schema" in low
        or "invalid json schema" in low
    )


def _usage_from_subscription(raw: dict[str, Any] | None) -> LLMUsage | None:
    """Нормализует usage из ``ResultMessage`` claude-agent-sdk.

    ``raw`` — ``{"usage": {...}, "total_cost_usd": ...}``. Возвращает None,
    если фактических токенов нет (тогда вызывающий применит оценку).
    """
    if not raw:
        return None
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    if input_tokens == 0 and output_tokens == 0:
        return None
    cache_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0) + int(
        usage.get("cache_read_input_tokens", 0) or 0
    )
    cost = raw.get("total_cost_usd")
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        source="actual",
        cache_tokens=cache_tokens or None,
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


@dataclass(frozen=True)
class ClaudeSubscriptionConfig:
    model: str | None
    max_turns: int = 1
    # Путь к ``claude`` CLI. None → попытаемся найти через PATH. Если и
    # этого нет — поднимаем ConflictError (НЕ используем bundled CLI,
    # потому что он не залогинен).
    cli_path: str | None = None
    # 1 час по умолчанию. На complex-задачах opus может думать 5-15 мин,
    # плюс initialize subprocess в Windows — медленный. Лучше big-ceiling.
    # Override: POV_CLAUDE_LOAD_TIMEOUT_MS (для load) и env
    # CLAUDE_CODE_STREAM_CLOSE_TIMEOUT (для initialize SDK control).
    load_timeout_ms: int = 3_600_000


def model_for_complexity(complexity: str | None) -> str | None:
    """Маппинг сложности на модель. None означает «модель по умолчанию CLI/подписки»."""
    overrides = {
        "trivial": os.environ.get("POV_CLAUDE_MODEL_TRIVIAL"),
        "standard": os.environ.get("POV_CLAUDE_MODEL_STANDARD"),
        "complex": os.environ.get("POV_CLAUDE_MODEL_COMPLEX"),
    }
    return overrides.get(complexity or "standard")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _iter_parseable_objects(text: str) -> list[tuple[dict[str, Any], str]]:
    """Найти все валидные JSON-объекты в тексте.

    Использует ``json.JSONDecoder.raw_decode`` от каждой позиции ``{``:
    если модель в subscription-CLI прервалась и стартовала JSON заново,
    одна из попыток обычно валидна. Возвращает список ``(parsed, raw_str)``,
    caller выберет самый длинный.

    Учитывает обе наши пост-обработки (``\\'`` → ``'``, ``strict=False``).
    """
    normalized = text.replace("\\'", "'")
    decoder = json.JSONDecoder(strict=False)
    results: list[tuple[dict[str, Any], str]] = []
    pos = 0
    while True:
        idx = normalized.find("{", pos)
        if idx < 0:
            break
        try:
            parsed, end = decoder.raw_decode(normalized, idx)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(parsed, dict):
            results.append((parsed, normalized[idx:end]))
        pos = end
    return results


class ClaudeSubscriptionClient:
    """Тонкая обёртка над claude-agent-sdk."""

    def __init__(self, config: ClaudeSubscriptionConfig) -> None:
        # Если cli_path не пришёл из конфига (например, прямой конструктор
        # в тестах), резолвим его здесь. Это гарантирует, что SDK НЕ
        # уйдёт в bundled CLI.
        if config.cli_path is None:
            config = dataclasses.replace(config, cli_path=_resolve_cli_path())
        # Подмешиваем env-переменную для SDK initialize timeout
        # (см. docstring _apply_initialize_timeout_env).
        _apply_initialize_timeout_env()
        self._config = config
        try:
            import claude_agent_sdk  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ConflictError(
                "Не установлен пакет 'claude-agent-sdk'. Поставьте его (pip install -e .[dev]) "
                "и убедитесь, что CLI 'claude' установлен и выполнен 'claude login'."
            ) from exc
        self._sdk = claude_agent_sdk
        # Нативный structured output появился в SDK как поле options —
        # детектируем по датаклассу (детерминированно, без TypeError-проб).
        self._sdk_supports_output_format = "output_format" in getattr(
            self._sdk.ClaudeAgentOptions, "__dataclass_fields__", {}
        )
        # Структурный режим этого инстанса: False после первой ошибки в нём
        # (downgrade-политика, см. chat_json).
        self._structured_disabled = False

    @classmethod
    def from_env(cls, *, model: str | None = None) -> "ClaudeSubscriptionClient":
        active_model = model or os.environ.get("POV_CLAUDE_MODEL") or None
        max_turns_raw = os.environ.get("POV_CLAUDE_MAX_TURNS", "1")
        try:
            max_turns = int(max_turns_raw)
        except ValueError as exc:
            raise ConflictError(
                f"POV_CLAUDE_MAX_TURNS должно быть целым числом, получено: {max_turns_raw}"
            ) from exc
        return cls(ClaudeSubscriptionConfig(
            model=active_model,
            max_turns=max_turns,
            cli_path=_resolve_cli_path(),
            load_timeout_ms=_resolve_load_timeout_ms(),
        ))

    @property
    def model(self) -> str:
        return self._config.model or "claude-code-default"

    def _cli_schema(self, schema: dict[str, Any]) -> dict[str, Any] | None:
        """Схема для флага ``--json-schema`` или None = структурный режим недоступен.

        None при: SDK без поля output_format / этот cli_path уже выяснил, что не
        знает флаг / этот инстанс деградировал после schema-специфичной ошибки /
        схема даже без описаний не влезает в лимит командной строки.
        """
        if not self._sdk_supports_output_format or self._structured_disabled:
            return None
        if self._config.cli_path and self._config.cli_path in _FLAG_UNSUPPORTED_CLIS:
            return None
        lean = strip_descriptions(schema)
        if len(json.dumps(lean, ensure_ascii=False)) > _CLI_SCHEMA_MAX_CHARS:
            logger.info(
                "claude_subscription: схема больше лимита CLI-аргумента — "
                "structured output пропущен, используется schema-в-промпте"
            )
            return None
        return lean

    def _disable_structured(self, message: str) -> None:
        """Отключить структурный режим после schema-специфичной ошибки.

        Для этого инстанса — всегда. Если ошибка указывает на отсутствие самого
        флага в CLI (``unknown option``) — кэшируем по cli_path на весь процесс,
        чтобы другие задачи на том же бинарнике не повторяли обречённую попытку.
        """
        self._structured_disabled = True
        # «unknown option» — однозначный сигнал, что САМ БИНАРНИК не знает флаг:
        # кэшируем по cli_path, чтобы все задачи на нём не пытались. Прочие
        # schema-ошибки (схему отверг режим) — проблема конкретного вызова, а не
        # бинарника: гасим только этот инстанс, чужие задачи не наказываем.
        if "unknown option" in message.lower():
            if self._config.cli_path:
                _FLAG_UNSUPPORTED_CLIS.add(self._config.cli_path)
            logger.warning(
                "claude_subscription: CLI не поддерживает --json-schema — "
                "enforcement отключён для этого бинарника до конца процесса"
            )
        else:
            logger.warning(
                f"claude_subscription: схема отвергнута структурным режимом — "
                f"продолжаем без него ({message[:120]})"
            )

    def _attempt(
        self,
        system_prompt: str,
        full_prompt: str,
        cli_schema: dict[str, Any] | None,
        token: CancellationToken | None,
    ) -> LLMResult:
        """Один вызов CLI (структурный, если ``cli_schema`` задан) → распарсенный
        payload + usage. Без retry-логики — ею управляет ``chat_json``."""
        text, raw_usage, structured = asyncio.run(
            self._collect_cancellable(system_prompt, full_prompt, cli_schema, token)
        )
        if isinstance(structured, dict):
            # Нативный structured output: payload уже распарсен CLI — без
            # regex-извлечения. null = «поле не заполнено» → каноника.
            payload = strip_nulls(structured)
        else:
            payload = self._extract_json(text)
        usage = _usage_from_subscription(raw_usage) or LLMUsage.estimated(
            input_text=system_prompt + full_prompt, output_text=text
        )
        return LLMResult(payload=payload, usage=usage)

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> LLMResult:
        # Схема дублируется в промпте независимо от структурного режима:
        # descriptions в ней — guidance по СОДЕРЖАНИЮ полей, а enforcement
        # (--json-schema) гарантирует только форму.
        full_prompt = (
            user_prompt
            + "\n\n---\n"
            + "Верни ответ строго в виде одного JSON-объекта, соответствующего этой JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
            + "\n\nНе добавляй пояснений вне JSON. Не используй markdown-обёртку."
        )
        # Retry с экспоненциальным backoff для транзиентных сбоев CLI.
        # Типичные транзиенты: "Command failed with exit code 1" (CLI вышел
        # без явной причины), "Control request timeout" (subprocess завис на
        # старте). Эти ошибки повторяются ~раз на 5-10 запусков —
        # сервер/процесс «отваливается», следующий вызов через 1-3 сек
        # обычно проходит. Без retry падает вся pipeline.
        attempts = max(int(os.environ.get("POV_CLAUDE_MAX_RETRIES", "3")), 1)
        last_exc: ConflictError | None = None
        # Токен отмены текущего шага (если исполнение идёт под runner'ом).
        # Берём из ambient-скоупа, чтобы не менять сигнатуру chat_json /
        # интерфейс LLMProvider. CancellationError — НЕ ConflictError,
        # поэтому отмена не попадает в retry-ветку и всплывает сразу.
        token = current_cancellation()
        import time as _time

        for attempt in range(1, attempts + 1):
            cli_schema = self._cli_schema(schema)
            try:
                return self._attempt(system_prompt, full_prompt, cli_schema, token)
            except ConflictError as exc:
                message = str(exc)
                # 1) Schema-специфичная ошибка (CLI не знает флаг / схему отверг
                #    структурный режим) → отключаем enforcement и тут же
                #    повторяем БЕЗ схемы в этой же попытке.
                if cli_schema is not None and _is_schema_mode_error(message):
                    self._disable_structured(message)
                    try:
                        return self._attempt(system_prompt, full_prompt, None, token)
                    except ConflictError as exc_plain:
                        exc, message = exc_plain, str(exc_plain)  # дальше — как обычная ошибка
                # 2) Транзиент и есть ещё попытки → backoff и повтор. Структурный
                #    режим СОХРАНЯЕТСЯ: сетевая флуктуация не повод терять
                #    enforcement (это и был главный баг).
                if _is_transient_cli_error(message) and attempt < attempts:
                    last_exc = exc
                    _time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s, ...
                    continue
                # 3) Попытки исчерпаны / ошибка не транзиентная. Если всё ещё были
                #    в структурном режиме — ПОСЛЕДНИЙ шанс БЕЗ enforcement: получить
                #    ответ важнее, чем enforcement на этом вызове (например, схема
                #    требует больше ходов, чем даёт max_turns). Структурный режим
                #    при этом НЕ гасим навсегда — для будущих задач он остаётся;
                #    форму этого ответа добьют нормализация + self-repair +
                #    валидация выше по конвейеру.
                if cli_schema is not None and not self._structured_disabled:
                    logger.warning(
                        f"claude_subscription: структурный режим не дался "
                        f"({message[:100]}) — финальная попытка без enforcement"
                    )
                    try:
                        return self._attempt(system_prompt, full_prompt, None, token)
                    except ConflictError:
                        pass
                raise
        # Недостижимо: последняя попытка либо вернула результат, либо raise выше.
        assert last_exc is not None
        raise last_exc

    async def _collect_cancellable(
        self,
        system_prompt: str,
        user_prompt: str,
        cli_schema: dict[str, Any] | None,
        token: CancellationToken | None,
    ) -> tuple[str, dict[str, Any] | None, Any]:
        """Запустить ``_collect`` с возможностью форсированной отмены.

        Без токена — обычный await. С токеном — оборачиваем сбор в asyncio-
        таску и подписываемся на отмену: при ``token.cancel()`` из другого
        потока (HTTP-обработчик) безопасно отменяем таску через
        ``loop.call_soon_threadsafe`` — это корректный кросс-тред способ
        прервать asyncio. Отмена таски рвёт ``async for`` по ``query`` →
        SDK закрывает CLI-subprocess. Получение ответа LLM прекращается, не
        дожидаясь завершения.
        """
        if token is None:
            return await self._collect(system_prompt, user_prompt, cli_schema)

        loop = asyncio.get_running_loop()
        collect_task: asyncio.Task[tuple[str, dict[str, Any] | None, Any]] = asyncio.ensure_future(
            self._collect(system_prompt, user_prompt, cli_schema)
        )
        unregister = token.register(
            lambda: loop.call_soon_threadsafe(collect_task.cancel)
        )
        try:
            return await collect_task
        except asyncio.CancelledError as exc:
            if token.is_cancelled:
                raise CancellationError("LLM-вызов прерван пользователем.") from exc
            raise
        finally:
            unregister()

    async def _collect(
        self, system_prompt: str, user_prompt: str, cli_schema: dict[str, Any] | None = None
    ) -> tuple[str, dict[str, Any] | None, Any]:
        # ClaudeAgentOptions может не иметь поля `model` в старых версиях SDK.
        # cli_path обязателен — он направляет SDK на залогиненный системный CLI
        # вместо bundled (см. docstring модуля).
        #
        # ВСЕГДА пишем system_prompt в файл. Причина: на Windows CLI
        # запускается через CMD shim (`claude.CMD`), и cmd.exe ломает
        # любые аргументы с символами `<`, `>` (redirect), переводами
        # строк, `%`, `^`, `&`, `|`. Наши system_prompt'ы реально содержат
        # XML-теги вида `<role>`, `<writing_principles>` и многострочные
        # секции — `--system-prompt <text>` через CMD не работает. Файл
        # передаётся через `--system-prompt-file <path>` (это путь, без
        # спецсимволов) и читается CLI стримом — стопроцентно надёжно.
        # Накладные расходы на запись/удаление файла — миллисекунды,
        # размер prompt'а уже не имеет значения.
        sp_tmpfile = _write_temp_prompt(system_prompt) if system_prompt else None
        sp_for_options: Any = (
            {"type": "file", "path": str(sp_tmpfile)} if sp_tmpfile is not None else ""
        )

        options_kwargs: dict[str, Any] = {
            "system_prompt": sp_for_options,
            "max_turns": self._config.max_turns,
            "permission_mode": "bypassPermissions",
            "cli_path": self._config.cli_path,
            "load_timeout_ms": self._config.load_timeout_ms,
        }
        if self._config.model:
            options_kwargs["model"] = self._config.model
        if cli_schema is not None:
            # Нативный structured output: SDK передаст схему CLI-флагом
            # ``--json-schema``, payload вернётся в ResultMessage.structured_output.
            options_kwargs["output_format"] = {"type": "json_schema", "schema": cli_schema}
        try:
            options = self._sdk.ClaudeAgentOptions(**options_kwargs)
        except TypeError:
            # Если какое-то поле не поддерживается старой версией SDK —
            # последовательно отбрасываем малозначимые и пробуем снова.
            for fallback_key in ("load_timeout_ms", "model", "cli_path"):
                options_kwargs.pop(fallback_key, None)
                try:
                    options = self._sdk.ClaudeAgentOptions(**options_kwargs)
                    break
                except TypeError:
                    continue
            else:
                # Последняя попытка с минимальным набором.
                options = self._sdk.ClaudeAgentOptions(
                    system_prompt=system_prompt,
                    max_turns=self._config.max_turns,
                    permission_mode="bypassPermissions",
                )

        chunks: list[str] = []
        raw_usage: dict[str, Any] | None = None
        structured_output: Any = None
        try:
            async for message in self._sdk.query(prompt=user_prompt, options=options):
                # ResultMessage в конце стрима несёт фактический usage и
                # total_cost_usd. Раньше он молча пропускался (нет content) —
                # теперь читаем токены отсюда (оценка остаётся только fallback).
                message_usage = getattr(message, "usage", None)
                if message_usage is not None:
                    raw_usage = {
                        "usage": message_usage,
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                    }
                # Structured output (если запрошен через --json-schema) — тоже
                # на ResultMessage: уже распарсенный объект по схеме.
                message_structured = getattr(message, "structured_output", None)
                if message_structured is not None:
                    structured_output = message_structured
                content = getattr(message, "content", None)
                if not content:
                    continue
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
        except Exception as exc:  # pragma: no cover
            msg = str(exc)
            if "Control request timeout" in msg and "initialize" in msg:
                raise ConflictError(
                    "Claude CLI не отвечает на initialize-запрос. Возможные причины:\n"
                    "• CLI не залогинен — выполните `claude login`.\n"
                    "• SDK использует bundled CLI вместо системного — задайте "
                    "POV_CLAUDE_CLI_PATH (см. `where claude`).\n"
                    "• Сильно медленный старт CLI (антивирус, диск). Увеличьте "
                    "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT (мс).\n"
                    f"Диагностика: cli_path={self._config.cli_path or '<не задан>'}; "
                    f"system_prompt={len(system_prompt)} chars (file mode)."
                ) from exc
            if "Command failed with exit code" in msg:
                raise ConflictError(
                    "Claude CLI завершился с ошибкой (exit code != 0). Это часто "
                    "транзиентный сбой подписочного API или процесса. После 3 retry "
                    "ничего не помогло. Возможные причины:\n"
                    "• Временный сбой claude.ai (5xx) — повторите через минуту.\n"
                    "• Превышен rate-limit подписки.\n"
                    "• Антивирус прибивает CLI subprocess.\n"
                    f"Диагностика: {msg[:200]}"
                ) from exc
            if "returned an error result" in msg:
                raise ConflictError(
                    "Claude CLI отдал result с пометкой error, но без конкретных "
                    "сообщений (часто subtype=success — противоречивый ответ "
                    "подписки). После 3 retry ничего не изменилось. Возможные причины:\n"
                    "• Транзиентный баг claude.ai — повторите через минуту.\n"
                    "• Истёк токен сессии — попробуйте `claude login` ещё раз.\n"
                    "• Превышен rate-limit подписки.\n"
                    f"Диагностика: {msg[:200]}"
                ) from exc
            raise ConflictError(f"Ошибка при обращении к Claude через подписку: {exc}") from exc
        finally:
            if sp_tmpfile is not None:
                try:
                    sp_tmpfile.unlink(missing_ok=True)
                except OSError:
                    pass
        return "".join(chunks), raw_usage, structured_output

    @staticmethod
    def _format_load_timeout_msg(seconds: int) -> str:  # for tests
        return f"load_timeout_ms={seconds * 1000}"

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ConflictError("Claude вернул пустой ответ.")
        # 1. Сначала пытаемся вытащить из markdown-блока ```json```.
        match = _JSON_FENCE_RE.search(text)
        if match:
            fenced = match.group(1).replace("\\'", "'")
            try:
                parsed = json.loads(fenced, strict=False)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        # 2. Иначе ищем все валидные JSON-объекты в тексте и берём самый
        #    длинный. Subscription-CLI на длинных ответах иногда стримит
        #    несколько JSON-объектов подряд (модель обрывается посередине
        #    и перезапускается с нуля — оба склеиваются в один поток).
        candidates = _iter_parseable_objects(text)
        if candidates:
            parsed, raw = max(candidates, key=lambda pair: len(pair[1]))
            return parsed
        # 3. Никаких валидных JSON не нашли — диагностика по «жадному»
        #    срезу `{...}` от первой `{` до последней `}` (исторически
        #    самый информативный candidate для логов).
        start = text.find("{")
        end = text.rfind("}")
        fallback = (
            text[start : end + 1].replace("\\'", "'") if start >= 0 and end > start else text
        )
        try:
            json.loads(fallback, strict=False)
        except json.JSONDecodeError as exc:
            raise ConflictError(
                f"Невалидный JSON в ответе Claude (line {exc.lineno} "
                f"col {exc.colno}): {exc.msg}. Candidate: {fallback!r}"
            ) from exc
        raise ConflictError(f"Не удалось извлечь JSON из ответа: {text!r}")


def _resolve_cli_path() -> str | None:
    """Найти путь к залогиненному системному `claude` CLI.

    Приоритеты:
    1. ``POV_CLAUDE_CLI_PATH`` — явный override (для нестандартных установок).
    2. ``shutil.which("claude")`` — стандартный поиск через PATH. На Windows
       это вернёт `.cmd`-shim, который запустит npm-установку.
    3. ``None`` — не нашли. SDK тогда уйдёт в свой bundled CLI; вызов
       почти наверняка упадёт по таймауту (bundled не залогинен). Мы это
       состояние ловим в ``_collect`` и даём пользователю осмысленный
       совет в тексте ошибки.
    """
    override = os.environ.get("POV_CLAUDE_CLI_PATH")
    if override and os.path.exists(override):
        return override
    found = shutil.which("claude")
    return found  # может быть None — это OK, обработаем в run-time


def _write_temp_prompt(text: str) -> Path:
    """Записать system_prompt во временный UTF-8 файл и вернуть путь.

    Используется ВСЕГДА, не по порогу (см. _collect). Файл будет удалён
    в finally блока вызывающего кода. Открываем с delete=False, потому
    что мы держим путь, а не handle — Windows иначе залочит файл от
    CLI-subprocess'а.
    """
    fd, path = tempfile.mkstemp(prefix="pov_claude_sysprompt_", suffix=".txt", text=False)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return Path(path)


def _resolve_load_timeout_ms() -> int:
    """Таймаут load_timeout_ms для session_resume и нашей выставленной
    верхней границы. Дефолт — 1 час (3 600 000 мс), потому что:

    * Complex-задачи opus думают 5-15 мин, бывает и дольше.
    * Windows initialize-spawn медленный (антивирус, диск).
    * Лучше большой ceiling, чем пять «retry-кнопок» в UI.

    Override через ``POV_CLAUDE_LOAD_TIMEOUT_MS``. Параллельно мы
    также подмешиваем env ``CLAUDE_CODE_STREAM_CLOSE_TIMEOUT``, который
    SDK реально использует для initialize control request (см.
    ``_apply_initialize_timeout_env``).
    """
    raw = os.environ.get("POV_CLAUDE_LOAD_TIMEOUT_MS", "3600000")
    try:
        return max(int(raw), 60_000)  # минимум 60s — SDK всё равно округлит до 60.
    except (TypeError, ValueError):
        return 3_600_000


def _is_transient_cli_error(message: str) -> bool:
    """Эвристика: ошибка похожа на транзиентный сбой CLI/подписки?

    На таких ошибках имеет смысл retry с backoff:
    * "Command failed with exit code N" — CLI вышел без явной причины
      (типично для проблем с claude.ai сервером или процессом).
    * "Control request timeout: initialize" — subprocess завис на старте.
    * "Process exited" / "Broken pipe" — обрыв связи.
    * "Claude Code returned an error result" — CLI отдал result с
      is_error=true, но без структурированного errors-списка (часто
      subtype="success" — противоречивая response от подписки).
      Транзиентный API-баг, повтор обычно проходит.

    НЕ retry'им (не транзиентно):
    * "Не задан POV_..." — конфигурация пустая.
    * "У connection пустой API key" — нужен ввод от админа.
    * "Не удалось извлечь JSON" — ответ модели не парсится, retry не поможет.
    """
    if not message:
        return False
    msg = message.lower()
    transient_markers = (
        "command failed with exit code",
        "control request timeout",
        "process exited",
        "broken pipe",
        "connection reset",
        "timed out",
        "returned an error result",  # включая абсурдный "error result: success"
    )
    return any(marker in msg for marker in transient_markers)


def _apply_initialize_timeout_env() -> None:
    """Подмешать ``CLAUDE_CODE_STREAM_CLOSE_TIMEOUT`` в env, если не задан.

    SDK читает эту env (в мс) для timeout'а initialize-control-request.
    Если пользователь не задал явно — ставим 1 час. Не перезатираем
    существующее значение."""
    if not os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"):
        os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "3600000"
