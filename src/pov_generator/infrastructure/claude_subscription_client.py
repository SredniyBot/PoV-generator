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

Ограничение: structured output через tool-use здесь не используется
(это сложно реализовать через агентскую SDK). Вместо этого мы просим
модель вернуть строго JSON и затем извлекаем его из ответа.
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

from ..common.errors import ConflictError


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

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
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
        for attempt in range(1, attempts + 1):
            try:
                text = asyncio.run(self._collect(system_prompt, full_prompt))
                return self._extract_json(text)
            except ConflictError as exc:
                if not _is_transient_cli_error(str(exc)) or attempt == attempts:
                    raise
                last_exc = exc
                # 1s, 2s, 4s, ... backoff
                import time as _time
                _time.sleep(2 ** (attempt - 1))
        # Недостижимо — последний attempt в except поднимает.
        assert last_exc is not None
        raise last_exc

    async def _collect(self, system_prompt: str, user_prompt: str) -> str:
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
        try:
            async for message in self._sdk.query(prompt=user_prompt, options=options):
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
        return "".join(chunks)

    @staticmethod
    def _format_load_timeout_msg(seconds: int) -> str:  # for tests
        return f"load_timeout_ms={seconds * 1000}"

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ConflictError("Claude вернул пустой ответ.")
        match = _JSON_FENCE_RE.search(text)
        candidate: str | None = None
        if match:
            candidate = match.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                candidate = text[start : end + 1]
        if candidate is None:
            raise ConflictError(f"Не удалось извлечь JSON из ответа: {text!r}")
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"Невалидный JSON в ответе Claude: {candidate!r}") from exc
        if not isinstance(payload, dict):
            raise ConflictError(f"Ожидался JSON-объект, получено: {payload!r}")
        return payload


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
