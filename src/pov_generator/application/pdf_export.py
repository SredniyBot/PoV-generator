"""Конвертер артефакт-маркдауна в PDF.

Идея: артефакт хранится как JSON + параллельный `.md` (рендер из
`artifact_contracts.render_markdown`). Для скачивания PDF мы берём
готовый markdown, прогоняем через `markdown` → HTML, и отдаём в
`xhtml2pdf` → bytes.

Все зависимости — pure-Python, без нативных библиотек (cairo / pango),
поэтому работает одинаково на Linux / macOS / Windows.

Cyrillic: дефолтные core-fonts PDF (Helvetica/Times/Courier) не покрывают
кириллицу. На каждой ОС есть штатный Unicode-TTF; модуль пытается
зарегистрировать его и подставить в стили. Override — через env-
переменную ``POV_PDF_FONT_PATH``.
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
from xhtml2pdf import pisa

from ..common.errors import PovGeneratorError

logger = logging.getLogger(__name__)


# Имя font-family, под которым мы регистрируем системную TTF.
_PDF_FONT_NAME = "PovBodyFont"
_PDF_FONT_NAME_BOLD = "PovBodyFont-Bold"


def render_artifact_pdf(
    *,
    markdown_content: str,
    title: str | None = None,
) -> bytes:
    """Сконвертировать markdown артефакта в PDF и вернуть байты.

    Args:
        markdown_content: исходный markdown (из artifact.markdown_content).
        title: заголовок страницы (HTML `<title>`), опционально.

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

    font_face_css = _build_font_face_css()
    css = _build_base_css(font_face_css)

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


# --- внутреннее: подбор шрифта ------------------------------------------------


def _build_font_face_css() -> str:
    """Регистрируем системную TTF под именем ``PovBodyFont`` и возвращаем
    соответствующие ``@font-face``-правила для xhtml2pdf.
    """
    regular_path, bold_path = _resolve_font_paths()
    if not regular_path:
        # Fallback: оставляем дефолтную Helvetica. Кириллица будет
        # отображаться плохо, но PDF всё равно сгенерируется. Логируем
        # для оператора.
        logger.warning(
            "PDF export: не найден Cyrillic-capable TTF. "
            "Задайте POV_PDF_FONT_PATH=<path-to-ttf>, иначе кириллица "
            "будет отображаться некорректно."
        )
        return ""

    try:
        pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME, str(regular_path)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF export: не удалось зарегистрировать %s: %s", regular_path, exc)
        return ""

    bold_face = ""
    if bold_path:
        try:
            pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME_BOLD, str(bold_path)))
            bold_face = (
                f"@font-face {{ font-family: '{_PDF_FONT_NAME}'; "
                f"src: url('{_uri(bold_path)}'); font-weight: bold; }}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF export: не удалось зарегистрировать %s: %s", bold_path, exc)

    return (
        f"@font-face {{ font-family: '{_PDF_FONT_NAME}'; "
        f"src: url('{_uri(regular_path)}'); }}"
        f"{bold_face}"
    )


def _uri(path: Path) -> str:
    """Конвертирует абсолютный путь в file:// URI (для CSS src: url(...))."""
    return path.resolve().as_uri()


@lru_cache(maxsize=1)
def _resolve_font_paths() -> tuple[Path | None, Path | None]:
    """Найти на системе пару (regular, bold) Cyrillic-capable TTF.

    Порядок:
    1. ``POV_PDF_FONT_PATH`` (+ опц. ``POV_PDF_FONT_BOLD_PATH``).
    2. Платформо-зависимые дефолты.
    3. Возврат (None, None) — fallback на встроенные шрифты xhtml2pdf.
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


def _build_base_css(font_face_css: str) -> str:
    body_family = f"'{_PDF_FONT_NAME}', Helvetica, sans-serif" if font_face_css else "Helvetica, sans-serif"
    return f"""
        {font_face_css}
        @page {{
            size: A4;
            margin: 2cm 1.8cm;
        }}
        body {{
            font-family: {body_family};
            font-size: 10.5pt;
            line-height: 1.4;
            color: #1c1c1c;
        }}
        h1 {{ font-size: 18pt; margin: 0 0 12pt 0; }}
        h2 {{ font-size: 14pt; margin: 14pt 0 6pt 0; border-bottom: 0.5pt solid #888; padding-bottom: 2pt; }}
        h3 {{ font-size: 12pt; margin: 10pt 0 4pt 0; }}
        h4 {{ font-size: 11pt; margin: 8pt 0 3pt 0; }}
        p {{ margin: 0 0 6pt 0; }}
        ul, ol {{ margin: 0 0 6pt 16pt; padding: 0; }}
        li {{ margin: 0 0 3pt 0; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 6pt 0 10pt 0;
            font-size: 9.5pt;
        }}
        th, td {{
            border: 0.5pt solid #999;
            padding: 4pt 6pt;
            vertical-align: top;
            text-align: left;
        }}
        th {{ background-color: #eef0f3; font-weight: bold; }}
        code {{
            font-family: 'Courier', monospace;
            font-size: 9.5pt;
            background-color: #f3f3f5;
            padding: 1pt 3pt;
        }}
        pre {{
            background-color: #f3f3f5;
            padding: 6pt 8pt;
            font-family: 'Courier', monospace;
            font-size: 9pt;
            white-space: pre-wrap;
        }}
        blockquote {{
            border-left: 2pt solid #b0b0b0;
            margin: 6pt 0;
            padding: 2pt 8pt;
            color: #444;
            font-style: italic;
        }}
        hr {{ border: 0; border-top: 0.5pt solid #999; margin: 10pt 0; }}
    """
