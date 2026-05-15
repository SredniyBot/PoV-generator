"""Конвертер артефакт-маркдауна в PDF.

Идея: артефакт хранится как JSON + параллельный `.md` (рендер из
`artifact_contracts.render_markdown`). Для скачивания PDF мы берём
готовый markdown, прогоняем через `markdown` → HTML, и отдаём в
`xhtml2pdf` → bytes.

Все зависимости — pure-Python, без нативных библиотек (cairo / pango),
поэтому работает одинаково на Linux / macOS / Windows.

Cyrillic: дефолтные core-fonts PDF (Helvetica/Times/Courier) не покрывают
кириллицу — без замены шрифта получим чёрные квадраты. Модуль ищет на
системе подходящий Unicode TTF, регистрирует его напрямую в reportlab
через ``pdfmetrics.registerFont`` (+ ``registerFontFamily`` для bold),
и подставляет имя зарегистрированного семейства в CSS ``font-family``.

Важно: мы НЕ используем CSS ``@font-face`` — xhtml2pdf плохо
резолвит ``src: url(file://...)`` для произвольных путей. Напрямую
зарегистрированный в reportlab font подхватывается xhtml2pdf по имени.

Override пути к шрифту — env-переменная ``POV_PDF_FONT_PATH`` (+ опц.
``POV_PDF_FONT_BOLD_PATH``).
"""

from __future__ import annotations

import io
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

import markdown as md_lib
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import default as _xhtml2pdf_default
from xhtml2pdf import pisa

from ..common.errors import PovGeneratorError

logger = logging.getLogger(__name__)

# Имя font-family, под которым мы регистрируем системную TTF в reportlab.
# xhtml2pdf использует то же имя через CSS ``font-family``.
_PDF_FONT_NAME = "PovBodyFont"
_PDF_FONT_NAME_BOLD = "PovBodyFont-Bold"
_PDF_FONT_FALLBACK = "Helvetica"  # core PDF font; для Cyrillic непригоден


def render_artifact_pdf(
    *,
    markdown_content: str,
    title: str | None = None,
) -> bytes:
    """Сконвертировать markdown артефакта в PDF и вернуть байты.

    Args:
        markdown_content: исходный markdown (из artifact.markdown_content).
        title: заголовок страницы (HTML ``<title>``), опционально.

    Returns:
        PDF-документ в виде bytes.

    Raises:
        PovGeneratorError: если HTML→PDF конверсия упала.
    """
    html_body = md_lib.markdown(
        markdown_content,
        extensions=[
            "extra",          # tables, fenced_code, footnotes, attr_list
            "sane_lists",
            "toc",
        ],
        output_format="xhtml",
    )

    body_font = _ensure_body_font_registered()
    css = _build_base_css(body_font)

    page_title = (title or "Artifact").replace("<", "&lt;").replace(">", "&gt;")
    html_document = (
        "<!DOCTYPE html>"
        '<html lang="ru"><head>'
        '<meta charset="utf-8"/>'
        f"<title>{page_title}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        f"{html_body}"
        "</body></html>"
    )

    buffer = io.BytesIO()
    pisa_status = pisa.CreatePDF(
        src=html_document,
        dest=buffer,
        encoding="utf-8",
    )
    if pisa_status.err:
        raise PovGeneratorError(
            f"Не удалось сгенерировать PDF (xhtml2pdf ошибок: {pisa_status.err})."
        )
    return buffer.getvalue()


# --- внутреннее: регистрация шрифта ------------------------------------------


@lru_cache(maxsize=1)
def _ensure_body_font_registered() -> str:
    """Зарегистрировать в reportlab Unicode-шрифт под именем ``PovBodyFont``.

    Возвращает реальное имя font-family, которое можно использовать в
    CSS — либо ``"PovBodyFont"`` (если шрифт нашли и зарегистрировали),
    либо ``"Helvetica"`` (fallback; кириллица будет ломаться, оператора
    предупреждаем в лог).

    Кэшируется на процесс — повторная регистрация одного и того же
    имени в reportlab приводит к WARNING.
    """
    regular_path, bold_path = _resolve_font_paths()
    if regular_path is None:
        logger.warning(
            "PDF export: не найден Unicode TTF на системе. "
            "Кириллица в PDF будет отображаться некорректно. "
            "Задайте POV_PDF_FONT_PATH=<путь к ttf> и при желании "
            "POV_PDF_FONT_BOLD_PATH=<путь к bold ttf>."
        )
        return _PDF_FONT_FALLBACK

    try:
        pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME, str(regular_path)))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PDF export: не удалось зарегистрировать %s в reportlab: %s. "
            "Откатываюсь к Helvetica.",
            regular_path,
            exc,
        )
        return _PDF_FONT_FALLBACK

    bold_registered = False
    if bold_path is not None:
        try:
            pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME_BOLD, str(bold_path)))
            bold_registered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PDF export: не удалось зарегистрировать bold-вариант %s: %s. "
                "Жирный текст останется regular.",
                bold_path,
                exc,
            )

    # Связываем regular + bold в одно семейство, чтобы <strong> / <b> /
    # font-weight:bold автоматически переключались на bold-вариант.
    bold_alias = _PDF_FONT_NAME_BOLD if bold_registered else _PDF_FONT_NAME
    pdfmetrics.registerFontFamily(
        _PDF_FONT_NAME,
        normal=_PDF_FONT_NAME,
        bold=bold_alias,
        italic=_PDF_FONT_NAME,
        boldItalic=bold_alias,
    )

    # КЛЮЧЕВОЙ ШАГ: xhtml2pdf держит собственный font-registry в
    # ``xhtml2pdf.default.DEFAULT_FONT`` и резолвит CSS ``font-family``
    # через него — НЕ через прямой lookup в reportlab. Если оставить
    # стандартные значения (helvetica → Helvetica), xhtml2pdf будет
    # рендерить core-PDF-шрифтом без кириллических глифов → чёрные
    # квадраты. Переопределяем все алиасы, которые ссылаются на
    # core-fonts без Unicode-покрытия, на наш зарегистрированный шрифт.
    default_map = _xhtml2pdf_default.DEFAULT_FONT
    _replace_core_fonts(default_map, replacement_regular=_PDF_FONT_NAME, replacement_bold=bold_alias)

    logger.info(
        "PDF export: зарегистрирован шрифт %s (regular=%s, bold=%s)",
        _PDF_FONT_NAME,
        regular_path,
        bold_path if bold_registered else "<нет>",
    )
    return _PDF_FONT_NAME


def _replace_core_fonts(
    default_map: dict[str, str],
    *,
    replacement_regular: str,
    replacement_bold: str,
) -> None:
    """Переопределить алиасы в ``xhtml2pdf.default.DEFAULT_FONT``.

    Все значения, указывающие на core-PDF-шрифты без Unicode-покрытия
    (Helvetica / Times-Roman / Courier и их bold/italic-варианты),
    заменяются на наш зарегистрированный Unicode-шрифт.

    Symbol и ZapfDingbats оставляем (это legacy-шрифты для математики/
    значков, в обычном тексте не встречаются).
    """
    non_unicode_targets = {
        "Helvetica",
        "Times-Roman",
        "Courier",
    }
    non_unicode_bold_targets = {
        "Helvetica-Bold",
        "Helvetica-BoldOblique",
        "Times-Bold",
        "Times-BoldOblique",
        "Courier-Bold",
        "Courier-BoldOblique",
    }
    # Оставляем как есть только Symbol и ZapfDingbats.
    for key, value in list(default_map.items()):
        if value in non_unicode_targets:
            default_map[key] = replacement_regular
        elif value in non_unicode_bold_targets:
            default_map[key] = replacement_bold


@lru_cache(maxsize=1)
def _resolve_font_paths() -> tuple[Path | None, Path | None]:
    """Найти на системе пару (regular, bold) Unicode TTF.

    Порядок:
    1. ``POV_PDF_FONT_PATH`` (+ опц. ``POV_PDF_FONT_BOLD_PATH``).
    2. Платформо-зависимые дефолты (Win: Arial / Segoe UI / Tahoma;
       macOS: Arial Unicode / Arial; Linux: DejaVu / Liberation / Noto).
    3. ``(None, None)`` — fallback на встроенную Helvetica.
    """
    override = os.environ.get("POV_PDF_FONT_PATH")
    if override:
        regular = Path(override)
        if regular.exists():
            bold_override = os.environ.get("POV_PDF_FONT_BOLD_PATH")
            bold = Path(bold_override) if bold_override else None
            return regular, (bold if bold and bold.exists() else None)

    candidates: tuple[tuple[str, str | None], ...]
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts_root = Path(windir) / "Fonts"
        candidates = (
            (str(fonts_root / "arial.ttf"), str(fonts_root / "arialbd.ttf")),
            (str(fonts_root / "segoeui.ttf"), str(fonts_root / "segoeuib.ttf")),
            (str(fonts_root / "tahoma.ttf"), str(fonts_root / "tahomabd.ttf")),
        )
    elif sys.platform == "darwin":
        candidates = (
            ("/Library/Fonts/Arial Unicode.ttf", None),
            ("/System/Library/Fonts/Supplemental/Arial.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
        )
    else:  # Linux / other POSIX
        candidates = (
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ("/usr/share/fonts/TTF/DejaVuSans.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
            ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
            ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
             "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        )

    for regular_str, bold_str in candidates:
        regular = Path(regular_str)
        if regular.exists():
            bold = Path(bold_str) if bold_str and Path(bold_str).exists() else None
            return regular, bold

    return None, None


# --- внутреннее: CSS ---------------------------------------------------------


def _build_base_css(body_font: str) -> str:
    """Базовый CSS для PDF.

    Важно: ``font-family`` задаётся на ``body`` И на всех ключевых
    элементах (h1..h4, p, li, table, th, td, blockquote). xhtml2pdf
    не всегда наследует font-family корректно через каскад, поэтому
    задаём явно.

    Mono-шрифт (code / pre) — оставляем Courier как core PDF font;
    латиница в коде покрыта, а если в коде встречается кириллица —
    переключаем на body_font тоже.
    """
    return f"""
        @page {{
            size: A4;
            margin: 2cm 1.8cm;
        }}
        body {{
            font-family: {body_font};
            font-size: 10.5pt;
            line-height: 1.4;
            color: #1c1c1c;
        }}
        h1, h2, h3, h4, h5, h6 {{
            font-family: {body_font};
            font-weight: bold;
        }}
        h1 {{ font-size: 18pt; margin: 0 0 12pt 0; }}
        h2 {{ font-size: 14pt; margin: 14pt 0 6pt 0; border-bottom: 0.5pt solid #888; padding-bottom: 2pt; }}
        h3 {{ font-size: 12pt; margin: 10pt 0 4pt 0; }}
        h4 {{ font-size: 11pt; margin: 8pt 0 3pt 0; }}
        p {{
            font-family: {body_font};
            margin: 0 0 6pt 0;
        }}
        ul, ol {{ margin: 0 0 6pt 16pt; padding: 0; }}
        li {{
            font-family: {body_font};
            margin: 0 0 3pt 0;
        }}
        strong, b {{ font-family: {body_font}; font-weight: bold; }}
        em, i {{ font-family: {body_font}; font-style: italic; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 6pt 0 10pt 0;
            font-size: 9.5pt;
        }}
        th, td {{
            font-family: {body_font};
            border: 0.5pt solid #999;
            padding: 4pt 6pt;
            vertical-align: top;
            text-align: left;
        }}
        th {{ background-color: #eef0f3; font-weight: bold; }}
        code {{
            font-family: {body_font};
            font-size: 9.5pt;
            background-color: #f3f3f5;
            padding: 1pt 3pt;
        }}
        pre {{
            font-family: {body_font};
            background-color: #f3f3f5;
            padding: 6pt 8pt;
            font-size: 9pt;
            white-space: pre-wrap;
        }}
        blockquote {{
            font-family: {body_font};
            border-left: 2pt solid #b0b0b0;
            margin: 6pt 0;
            padding: 2pt 8pt;
            color: #444;
            font-style: italic;
        }}
        hr {{ border: 0; border-top: 0.5pt solid #999; margin: 10pt 0; }}
    """
