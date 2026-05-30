"""Кооперативная отмена для долгих операций (LLM-вызовы, шаги workflow).

Зачем
-----
Шаг workflow выполняется синхронно в фоновом потоке runner'а и может
надолго заблокироваться на получении ответа от LLM. Чтобы остановка работы
над проектом немедленно и безопасно прерывала текущий шаг, нужен механизм
отмены, который:

* **кооперативен** — оркестратор проверяет токен в безопасных точках и
  аккуратно сворачивается (никаких partial-результатов в БД);
* **форсирован там, где транспорт это позволяет** — провайдер может
  подписаться на отмену и оборвать конкретный ресурс (например, прибить
  CLI-subprocess / отменить asyncio-таску), не дожидаясь ответа LLM.

Дизайн
------
``CancellationToken`` — потокобезопасный сигнал «отменено» с реестром
коллбэков (Observer): подписчик получает уведомление в момент отмены и
может проактивно прервать свой ресурс. Это разделяет ответственность:
оркестратор решает *когда* отменять, а каждый ресурс — *как* прерваться.

``cancellation_scope`` / ``current_cancellation`` — ambient-скоуп на основе
``ContextVar``. Он позволяет провайдеру (нижний слой) узнать про активную
отмену, **не меняя сигнатуру ``LLMProvider.chat_json``**: интерфейс
провайдера остаётся чистым (ISP), а форсированное прерывание добавляется
точечно тем провайдерам, которые это умеют (OCP). Оркестратор устанавливает
скоуп явно из переданного ему токена — никакой скрытой глобальной мутации.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import PovGeneratorError

logger = logging.getLogger(__name__)


class CancellationError(PovGeneratorError):
    """Операция прервана через :class:`CancellationToken`.

    Отдельный тип (а не generic ``Exception``), чтобы оркестратор мог
    отличить осознанную отмену от настоящей ошибки исполнения и обработать
    её иначе (сбросить шаг в ``ready`` вместо ``failed``).
    """


class CancellationToken:
    """Потокобезопасный сигнал отмены с проактивными коллбэками.

    Однонаправленный: после ``cancel()`` остаётся отменённым навсегда.
    ``register`` подписывает коллбэк, который вызывается ровно один раз в
    момент отмены (или немедленно, если токен уже отменён) — этим
    пользуется провайдер, чтобы оборвать свой блокирующий ресурс.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Бросить :class:`CancellationError`, если отмена уже запрошена.

        Вызывается оркестратором в безопасных точках (перед началом работы,
        перед коммитом результата), чтобы свернуться без partial-состояния.
        """
        if self._event.is_set():
            raise CancellationError("Операция прервана пользователем.")

    def cancel(self) -> None:
        """Запросить отмену и уведомить подписчиков (идемпотентно)."""
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            self._invoke(callback)

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Подписать коллбэк на момент отмены.

        Возвращает функцию отписки — её нужно вызвать, когда ресурс
        освобождён штатно (например, после успешного ответа LLM), чтобы не
        копить мёртвые коллбэки. Если токен уже отменён — коллбэк
        вызывается немедленно, а отписка становится no-op.
        """
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def _unregister() -> None:
                    with self._lock:
                        try:
                            self._callbacks.remove(callback)
                        except ValueError:
                            pass

                return _unregister
        # Уже отменён — выполняем сразу, отписывать нечего.
        self._invoke(callback)
        return lambda: None

    @staticmethod
    def _invoke(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 — коллбэк отмены не должен ронять cancel()
            logger.warning("Коллбэк отмены завершился ошибкой.", exc_info=True)


# Ambient-скоуп активной отмены. Хранит токен текущего исполняемого шага,
# чтобы нижние слои (провайдеры) могли подписаться на форсированное
# прерывание, не получая токен через сигнатуры.
_current_cancellation: ContextVar[CancellationToken | None] = ContextVar(
    "current_cancellation", default=None
)


def current_cancellation() -> CancellationToken | None:
    """Токен отмены текущего скоупа, либо ``None``, если его нет."""
    return _current_cancellation.get()


@contextmanager
def cancellation_scope(token: CancellationToken | None) -> Iterator[CancellationToken | None]:
    """Установить ambient-токен отмены на время блока.

    ``token=None`` — корректный no-op (исполнение вне runner'а: CLI, тесты),
    при котором ``current_cancellation()`` вернёт прежнее значение.
    """
    reset_token = _current_cancellation.set(token)
    try:
        yield token
    finally:
        _current_cancellation.reset(reset_token)
