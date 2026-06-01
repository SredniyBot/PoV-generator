"""Централизованное логирование PoV Generator.

Цели (см. требования): по логам должно быть видно «что происходит прямо
сейчас», критические параметры критических операций, тайминг; уровни
разнесены; без спама; читать легко и приятно.

Дизайн
------
* **Структурированные поля.** `log.info("run started", run_id=..., provider=...)`
  — поля не зашиваются в строку, а рендерятся отдельно (`key=value`), что даёт
  единообразие и возможность json-вывода для агрегаторов.
* **Контекст через `contextvars`.** `bind(request_id=..., project_id=...)` —
  и все логи внутри блока несут эти поля без ручной передачи. Так трассируется
  и HTTP-запрос, и фоновый workflow-run (поток биндит свой контекст сам).
* **Уровни.** DEBUG — внутренности/высокочастотное; INFO — вехи жизненного
  цикла; WARNING — деградации/ретраи/отмена; ERROR — сбои. Уровень берётся из
  `POV_LOG_LEVEL` (default INFO).
* **Формат.** `POV_LOG_FORMAT=pretty` (default, человекочитаемый, цвет в TTY)
  или `json` (по строке-объекту на запись). Цвет можно выключить `POV_LOG_COLOR=off`.
* **Без спама.** Высокочастотные операции (поллинг-эндпоинты, попадания в кеш,
  отдельные записи в БД) логируются на DEBUG; на INFO — только содержательные вехи.

Эта точка — единственная, где конфигурируется логирование (`configure_logging`),
вызывается из `create_app` и из CLI `main`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# Все логгеры проекта живут под неймспейсом "pov.*" — изолируем настройку от
# uvicorn / сторонних библиотек.
_ROOT = "pov"

# Контекст текущего запроса/операции. Каждый поток/таск стартует со своей
# копией (contextvars), поэтому фоновые workflow-потоки не «протекают» друг в
# друга — они биндят контекст сами в начале своего run-loop.
_log_context: ContextVar[dict[str, Any]] = ContextVar("pov_log_context", default={})

_configured = False

# ---- контекст ------------------------------------------------------------


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Добавить поля в контекст логирования на время блока.

    Вложенные `bind` накапливают поля; по выходу контекст восстанавливается.
    Значения `None` игнорируются (удобно для опциональных полей).
    """
    clean = {k: v for k, v in fields.items() if v is not None}
    token = _log_context.set({**_log_context.get(), **clean})
    try:
        yield
    finally:
        _log_context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_log_context.get())


def new_request_id() -> str:
    """Короткий идентификатор для трассировки одного запроса/операции."""
    return uuid.uuid4().hex[:8]


# ---- структурированный логгер -------------------------------------------


class StructuredLogger:
    """Тонкая обёртка над stdlib-логгером с `**fields` вместо %-форматирования.

    Поля складываются в `record.pov_fields` и рендерятся форматтером. Проверка
    `isEnabledFor` до сборки — чтобы на отключённом уровне не платить за
    форматирование.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _emit(self, level: int, msg: str, *, exc_info: bool = False, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        clean = {k: v for k, v in fields.items() if v is not None}
        self._logger.log(level, msg, extra={"pov_fields": clean}, exc_info=exc_info)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, msg, **fields)

    def error(self, msg: str, *, exc_info: bool = True, **fields: Any) -> None:
        # По умолчанию прикладываем трейс — error без причины бесполезен.
        self._emit(logging.ERROR, msg, exc_info=exc_info, **fields)

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)


def get_logger(area: str) -> StructuredLogger:
    """Получить логгер по короткому имени области (`runner`, `http`, ...).

    Имя нормализуется в `pov.<area>`; полное имя оставляется как есть.
    """
    name = area if area.startswith(_ROOT) else f"{_ROOT}.{area}"
    return StructuredLogger(logging.getLogger(name))


# ---- тайминг -------------------------------------------------------------


@contextmanager
def log_duration(
    logger: StructuredLogger,
    msg: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> Iterator[None]:
    """Залогировать длительность блока (`duration_ms`).

    Успех → `msg` на заданном уровне; исключение → `{msg} failed` на ERROR с
    трейсом и длительностью до сбоя. Тайминг всегда виден.
    """
    start = time.perf_counter()
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 — логируем и пробрасываем
        dur = round((time.perf_counter() - start) * 1000)
        logger.error(f"{msg} failed", duration_ms=dur, error=str(exc) or type(exc).__name__, **fields)
        raise
    else:
        dur = round((time.perf_counter() - start) * 1000)
        logger._emit(level, msg, duration_ms=dur, **fields)


# ---- форматтеры ----------------------------------------------------------

_LEVEL_COLOR = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_DIM = "\033[2m"
_RESET = "\033[0m"

# Короткие подписи уровней (WARNING длинный — режем до WARN).
_LEVEL_LABEL = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT",
}

def _fmt_duration(ms: float) -> str:
    """Человекочитаемая длительность: 823мс / 1.8с / 2м 05с."""
    if ms < 1000:
        return f"{int(round(ms))}мс"
    sec = ms / 1000
    if sec < 60:
        return f"{sec:.1f}с"
    return f"{int(sec // 60)}м {int(sec % 60):02d}с"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    text = value if isinstance(value, str) else str(value)
    if any(c in text for c in (" ", "=", '"')):
        text = '"' + text.replace('"', '\\"') + '"'
    return text


def _render_fields(fields: dict[str, Any]) -> str:
    # Ключи полей оставляем английскими (provider=, status=, task=…) — так
    # привычнее и не выглядит странно; русскими остаются тексты сообщений.
    return ", ".join(f"{k}={_render_value(v)}" for k, v in fields.items())


class PovFormatter(logging.Formatter):
    """Компактный человекочитаемый формат:

        ``время УРОВЕНЬ область  сообщение  поля — длительность``

    Сознательно без UUID-контекста и полного имени логгера — они засоряли
    строку «случайным текстом». Трассировка (request_id/run_id/...) остаётся
    в json-режиме (``POV_LOG_FORMAT=json``); человеку важны действие и данные.
    """

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = _LEVEL_LABEL.get(record.levelno, record.levelname)
        area = record.name.split(".")[-1]  # pov.runner -> runner

        fields = dict(getattr(record, "pov_fields", {}) or {})
        dur = fields.pop("duration_ms", None)
        tail = _render_fields(fields)
        dur_s = f" — {_fmt_duration(dur)}" if isinstance(dur, (int, float)) else ""

        if self._color:
            level_s = f"{_LEVEL_COLOR.get(record.levelno, '')}{level:<5}{_RESET}"
            area_s = f"{_DIM}{area:<9}{_RESET}"
            ts_s = f"{_DIM}{ts}{_RESET}"
            tail_s = f"  {_DIM}{tail}{_RESET}" if tail else ""
        else:
            level_s = f"{level:<5}"
            area_s = f"{area:<9}"
            ts_s = ts
            tail_s = f"  {tail}" if tail else ""

        line = f"{ts_s} {level_s} {area_s} {record.getMessage()}{tail_s}{dur_s}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _DynamicStderrHandler(logging.StreamHandler):
    """StreamHandler, всегда пишущий в ТЕКУЩИЙ ``sys.stderr``.

    Базовый StreamHandler захватывает поток в ``__init__``. Под pytest (захват
    stderr) и при uvicorn ``--reload`` поток может быть подменён или закрыт, а
    daemon-поток runner'а продолжает логировать — итог «I/O on closed file».
    Резолвим поток лениво на каждый emit, чтобы этого не было.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:  # noqa: D401 — поток фиксирован на sys.stderr
        pass


class JsonLogFormatter(logging.Formatter):
    """Одна json-строка на запись — для агрегаторов (POV_LOG_FORMAT=json)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_log_context.get())
        payload.update(getattr(record, "pov_fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---- конфигурация --------------------------------------------------------


def configure_logging(*, force: bool = False) -> None:
    """Настроить логирование один раз за процесс (идемпотентно).

    Env:
      * ``POV_LOG_LEVEL``  — уровень (default INFO).
      * ``POV_LOG_FORMAT`` — ``pretty`` (default) | ``json``.
      * ``POV_LOG_COLOR``  — ``off`` чтобы отключить ANSI-цвет (иначе авто по TTY).
    """
    global _configured
    if _configured and not force:
        return

    level_name = os.environ.get("POV_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("POV_LOG_FORMAT", "pretty").lower()
    color_env = os.environ.get("POV_LOG_COLOR", "auto").lower()
    use_color = color_env not in {"off", "0", "false"} and sys.stderr.isatty()

    handler = _DynamicStderrHandler()
    handler.setFormatter(JsonLogFormatter() if fmt == "json" else PovFormatter(color=use_color))

    # Вешаем единый handler на КОРНЕВОЙ логгер, а не только на «pov.*». Так в
    # общий формат попадают и наши `get_logger("pov.*")`, и существующие ad-hoc
    # `logging.getLogger(__name__)` по всему `pov_generator.*` (mermaid, pdf,
    # decision-сервисы и т.п.) — без правки каждого call-site. PovFormatter
    # рендерит и %-сообщения (через record.getMessage()), и структурированные.
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Приглушаем шумные сторонние логгеры до WARNING, чтобы INFO не тонул в
    # их трафике (HTTP-клиенты, SDK, reload-watcher). uvicorn.access — глушим,
    # т.к. HTTP-запросы логирует наш middleware.
    for noisy in (
        "uvicorn.access",
        "httpx",
        "httpcore",
        "urllib3",
        "anthropic",
        "openai",
        "asyncio",
        "watchfiles",
        "mcp",
        "markdown_it",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
