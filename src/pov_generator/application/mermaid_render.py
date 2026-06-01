"""Серверный рендер Mermaid-диаграмм в PNG для встраивания в PDF.

UI использует `mermaid.js` в браузере; в PDF (xhtml2pdf, без JS) диаграммы
по умолчанию остаются code-block'ами. Чтобы получить графику в PDF, нужен
серверный рендер: запускаем `mmdc` (``@mermaid-js/mermaid-cli``, Node +
headless Chromium) как subprocess и возвращаем PNG-байты.

Графический рендер опционален: если `mmdc` не установлен или падает,
``render_mermaid_to_png`` возвращает ``None`` — вызывающий код оставляет
исходный ```mermaid``` блок как есть. Тесты включают
``POV_MERMAID_DISABLED=1`` чтобы коротко замкнуть путь без mock'ов.

Env-настройки:
* ``POV_MERMAID_CLI`` — путь/имя бинаря (по умолчанию ``mmdc``).
* ``POV_MERMAID_DISABLED`` — если задано не пусто, рендер всегда возвращает
  ``None``. Удобно в CI и dev-машинах без Node.
* ``POV_MERMAID_TIMEOUT`` — таймаут одного вызова в секундах (default ``30``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


_MAX_CACHE_ENTRIES = 256
_DEFAULT_TIMEOUT_SECONDS = 30
# Кеш по хэшу исходника — один сгенерированный PNG для одинаковых
# диаграмм. Хранится в памяти процесса; для нашего use-case (несколько
# диаграмм на документ, повторные скачивания того же артефакта) этого
# достаточно. Очищается через ``clear_cache`` в тестах.
_png_cache: dict[str, bytes] = {}


def _is_disabled() -> bool:
    return bool(os.environ.get("POV_MERMAID_DISABLED"))


def _mmdc_binary() -> str:
    return os.environ.get("POV_MERMAID_CLI", "mmdc")


def _local_bin_candidates() -> list[Path]:
    """Бинарь mmdc из локальной установки проекта.

    ``@mermaid-js/mermaid-cli`` объявлен optionalDependency UI-воркспейса и
    ставится штатным ``npm ci`` в ``ui/workspace/node_modules/.bin``. Это
    основной, воспроизводимый путь: на любой новой среде после стандартной
    установки UI mmdc доступен — без глобального ``npm i -g`` и возни с PATH.
    """
    # mermaid_render.py: <repo>/src/pov_generator/application/mermaid_render.py
    repo_root = Path(__file__).resolve().parents[3]
    node_modules = repo_root / "ui" / "workspace" / "node_modules"
    # Полнота установки: mmdc бесполезен без peer-зависимости puppeteer
    # (Chromium). Если её нет (частичная optional-установка) — не подсовываем
    # сломанный локальный бинарь, пусть сработает глобальный фоллбек.
    if not (node_modules / "puppeteer").exists():
        return []
    bin_dir = node_modules / ".bin"
    if sys.platform == "win32":
        # npm кладёт mmdc.cmd (его исполняет _build_command через `cmd /c`).
        return [bin_dir / "mmdc.cmd", bin_dir / "mmdc"]
    return [bin_dir / "mmdc"]


def _resolve_binary() -> str | None:
    """Найти исполняемый mmdc. Приоритет:

    1. ``POV_MERMAID_CLI`` — явный путь/имя (override).
    2. Локальная установка проекта (ui/workspace/node_modules/.bin) — основной
       путь, работает на любой среде после ``npm ci``.
    3. Глобальный mmdc в PATH. ``shutil.which`` учитывает PATHEXT (находит
       ``mmdc.cmd`` на Windows, где subprocess(shell=False) сам .cmd не ищет).
    """
    override = os.environ.get("POV_MERMAID_CLI")
    if override:
        return shutil.which(override) or (override if Path(override).exists() else None)
    for candidate in _local_bin_candidates():
        if candidate.exists():
            return str(candidate)
    return shutil.which("mmdc")


def _build_command(binary: str, args: list[str]) -> list[str]:
    """Собрать argv с учётом особенностей Windows.

    ``.cmd``/``.bat`` (как npm-обёртка ``mmdc.cmd``) CreateProcess напрямую не
    исполняет — заворачиваем в ``cmd /c``. На остальных платформах — как есть.
    """
    if sys.platform == "win32" and binary.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", binary, *args]
    return [binary, *args]


def _timeout_seconds() -> int:
    raw = os.environ.get("POV_MERMAID_TIMEOUT")
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


def clear_cache() -> None:
    """Очистить PNG-кеш. Используется в тестах."""
    _png_cache.clear()


def render_mermaid_to_png(source: str) -> bytes | None:
    """Сгенерировать PNG из Mermaid-исходника через ``mmdc``.

    Возвращает байты PNG или ``None``, если рендер недоступен / упал.
    Кеширует успешные результаты по SHA-256 от исходника.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    if _is_disabled():
        return None

    cache_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cached = _png_cache.get(cache_key)
    if cached is not None:
        return cached

    png = _invoke_mmdc(source)
    if png is None:
        return None

    if len(_png_cache) >= _MAX_CACHE_ENTRIES:
        # Грубое выселение: убираем произвольный первый элемент. Простой
        # FIFO достаточен — диаграммы дёшево перегенерить при кеш-промахе.
        try:
            first_key = next(iter(_png_cache))
            _png_cache.pop(first_key, None)
        except StopIteration:
            pass
    _png_cache[cache_key] = png
    return png


def _invoke_mmdc(source: str) -> bytes | None:
    binary = _resolve_binary()
    if binary is None:
        logger.info(
            "mermaid-cli (%s) не найден; PDF останется с code-block'ами. "
            "Установите: npm i -g @mermaid-js/mermaid-cli",
            _mmdc_binary(),
        )
        return None
    timeout = _timeout_seconds()
    with tempfile.TemporaryDirectory(prefix="povgen-mmdc-") as tmp_dir:
        input_path = Path(tmp_dir) / "diagram.mmd"
        output_path = Path(tmp_dir) / "diagram.png"
        input_path.write_text(source, encoding="utf-8")
        cmd = _build_command(
            binary,
            [
                "-i", str(input_path),
                "-o", str(output_path),
                # Прозрачный фон чтобы PNG ложился на любую страницу PDF.
                "-b", "transparent",
                # 2x масштаб — нормальная плотность для печати без размытия.
                "-s", "2",
            ],
        )
        try:
            result = subprocess.run(  # noqa: S603 — bin path резолвится через which
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            logger.info(
                "mermaid-cli (%s) не запустился (FileNotFound); PDF останется "
                "с code-block'ами.",
                binary,
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "mermaid-cli превысил таймаут %ds; диаграмма пропущена.", timeout
            )
            return None
        except OSError as exc:
            logger.warning("mermaid-cli не запустился: %s", exc)
            return None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                "mermaid-cli вернул код %s: %s",
                result.returncode,
                stderr[:500],
            )
            return None
        if not output_path.exists():
            logger.warning("mermaid-cli отработал, но PNG не создан.")
            return None
        return output_path.read_bytes()
