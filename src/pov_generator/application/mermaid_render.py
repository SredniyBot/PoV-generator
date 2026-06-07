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
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


_MAX_CACHE_ENTRIES = 256
_DEFAULT_TIMEOUT_SECONDS = 30
# Кеш по хэшу исходника — один сгенерированный результат для одинаковых
# диаграмм. Хранится в памяти процесса; для нашего use-case (несколько
# диаграмм на документ, повторные скачивания того же артефакта) этого
# достаточно. Очищается через ``clear_cache`` в тестах.
_png_cache: dict[str, bytes] = {}
_svg_cache: dict[str, str] = {}

# Mermaid с htmlLabels:false рендерит подписи как настоящий SVG <text>
# (а не foreignObject c HTML) — только такой SVG умеет конвертировать svglib,
# на котором держится SVG-поддержка xhtml2pdf. Иначе подписи пропадут.
_SVG_MERMAID_CONFIG = {"htmlLabels": False, "flowchart": {"htmlLabels": False}}

# Нулевой stroke-dasharray (например, у маркеров стрелок Mermaid) роняет
# reportlab («dash cycle should be larger than zero»). Убираем только полностью
# нулевые массивы; реальные пунктиры (есть ненулевая цифра) сохраняем.
_DASH_RE = re.compile(r"""stroke-dasharray\s*([:=])\s*(["']?)([^;"'>]*)\2""")


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
    """Очистить кеши (PNG и SVG). Используется в тестах."""
    _png_cache.clear()
    _svg_cache.clear()


def render_mermaid_to_svg(source: str) -> str | None:
    """Сгенерировать **векторный** SVG из Mermaid-исходника через ``mmdc``.

    Это предпочтительный формат для PDF (чёткость в печати, масштабируемость).
    Шаги: mmdc с ``htmlLabels:false`` → санитизация нулевого dash → проверка,
    что svglib/reportlab конвертирует SVG без ошибок. Если на любом шаге
    неудача — возвращаем ``None``, и вызывающий код падает на PNG (а затем на
    code-block). Так PDF никогда не ломается на «неудобной» диаграмме.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    if _is_disabled():
        return None

    cache_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cached = _svg_cache.get(cache_key)
    if cached is not None:
        return cached

    raw = _run_mmdc(source, suffix=".svg", extra_args=[], config=_SVG_MERMAID_CONFIG)
    if raw is None:
        return None
    svg = _sanitize_svg(raw.decode("utf-8", errors="replace"))
    if not _svg_converts_cleanly(svg):
        logger.info("Mermaid SVG не конвертируется чисто (svglib/reportlab); fallback на PNG.")
        return None

    _cache_put(_svg_cache, cache_key, svg)
    return svg


def render_mermaid_to_png(source: str) -> bytes | None:
    """Сгенерировать PNG из Mermaid-исходника через ``mmdc``.

    Возвращает байты PNG или ``None``, если рендер недоступен / упал.
    Кеширует успешные результаты по SHA-256 от исходника. Используется как
    надёжный запасной вариант, когда SVG недоступен.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    if _is_disabled():
        return None

    cache_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cached = _png_cache.get(cache_key)
    if cached is not None:
        return cached

    # 2x масштаб — нормальная плотность для печати без размытия.
    png = _run_mmdc(source, suffix=".png", extra_args=["-s", "2"])
    if png is None:
        return None

    _cache_put(_png_cache, cache_key, png)
    return png


def _cache_put(cache: dict, key: str, value) -> None:
    """Положить в кеш с грубым FIFO-выселением (диаграммы дёшево перегенерить)."""
    if len(cache) >= _MAX_CACHE_ENTRIES:
        try:
            cache.pop(next(iter(cache)), None)
        except StopIteration:
            pass
    cache[key] = value


def _sanitize_svg(svg: str) -> str:
    """Убрать полностью нулевые ``stroke-dasharray`` (роняют reportlab).

    Реальные пунктиры (есть ненулевая цифра) не трогаем.
    """
    def _repl(match: re.Match[str]) -> str:
        value = match.group(3)
        return match.group(0) if re.search(r"[1-9]", value) else ""

    return _DASH_RE.sub(_repl, svg)


def _svg_converts_cleanly(svg: str) -> bool:
    """Проверить, что SVG конвертируется svglib→reportlab без ошибок.

    Это гарантия, что встроенный в PDF SVG не уронит xhtml2pdf (который
    использует тот же svglib). Любая проблема (нет svglib, кривой SVG,
    несовместимая фигура) → False → fallback на PNG.
    """
    try:
        from reportlab.graphics import renderPDF
        from svglib.svglib import svg2rlg
    except Exception:
        return False
    try:
        drawing = svg2rlg(io.StringIO(svg))
        if drawing is None:
            return False
        renderPDF.drawToString(drawing)
        return True
    except Exception:
        return False


def _run_mmdc(
    source: str,
    *,
    suffix: str,
    extra_args: list[str],
    config: dict | None = None,
) -> bytes | None:
    """Запустить ``mmdc`` и вернуть байты результата (PNG/SVG) или ``None``.

    Общий subprocess-каркас для PNG и SVG: резолв бинаря, временные файлы,
    прозрачный фон, опциональный mermaid-config (``-c``), защита от таймаута/
    отсутствия бинаря. Любая неудача → ``None`` (graceful degradation).
    """
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
        tmp = Path(tmp_dir)
        input_path = tmp / "diagram.mmd"
        output_path = tmp / f"diagram{suffix}"
        input_path.write_text(source, encoding="utf-8")
        args = ["-i", str(input_path), "-o", str(output_path), "-b", "transparent", *extra_args]
        if config is not None:
            config_path = tmp / "mermaid-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args += ["-c", str(config_path)]
        cmd = _build_command(binary, args)
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
            logger.warning("mermaid-cli превысил таймаут %ds; диаграмма пропущена.", timeout)
            return None
        except OSError as exc:
            logger.warning("mermaid-cli не запустился: %s", exc)
            return None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.warning("mermaid-cli вернул код %s: %s", result.returncode, stderr[:500])
            return None
        if not output_path.exists():
            logger.warning("mermaid-cli отработал, но файл %s не создан.", suffix)
            return None
        return output_path.read_bytes()
