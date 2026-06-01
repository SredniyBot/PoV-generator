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

import base64
import io
import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

import markdown as md_lib
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import default as _xhtml2pdf_default
from xhtml2pdf import pisa

from ..common.errors import PovGeneratorError
from .mermaid_render import render_mermaid_to_png

logger = logging.getLogger(__name__)

# Имя font-family, под которым мы регистрируем системную TTF в reportlab.
# xhtml2pdf использует то же имя через CSS ``font-family``.
_PDF_FONT_NAME = "PovBodyFont"
_PDF_FONT_NAME_BOLD = "PovBodyFont-Bold"
_PDF_FONT_FALLBACK = "Helvetica"  # core PDF font; для Cyrillic непригоден

# --- размеры страниц (для оценки «лезет ли таблица в portrait») -------------
# A4 = 21.0 × 29.7 cm; 1 cm ≈ 28.346 pt.
# v3.8.3: landscape-поля сжаты до 1.0см с боков — это страница с
# таблицей, а не литературный текст; чем больше места под колонки, тем
# реже алгоритм скейлит мин-ширины вниз и рвёт слова. Portrait-поля
# оставлены щедрыми (1.8см) для обычных артефактных PDF.
# Доступная под контент ширина:
#   portrait  = (21.0 − 2×1.8) cm ≈ 493 pt
#   landscape = (29.7 − 2×1.0) cm ≈ 785 pt
_PORTRAIT_CONTENT_WIDTH_PT = 493.0
_LANDSCAPE_CONTENT_WIDTH_PT = 785.0

# Грубая оценка средней ширины символа в шрифте таблицы (9.5pt).
#
# v3.8.2: повышено с 5.5 до 6.2pt по результатам реальных PDF-рендеров.
# Причины:
#   - кириллица в Arial рендерится шире, чем латиница; буквы «ш», «щ», «м»,
#     «ю», «ы», «ж» дают ~6.5pt каждая, среднее по слову — около 5.8-6pt.
#   - жирный (`**word**` для выбранного варианта) рендерится ~15% шире
#     обычного начертания. Если жирный определяет longest_word, расчёт
#     по обычному char_width недооценивает реальную ширину.
#   - запас на «безопасном допущении» при оценке очень нужен — иначе
#     слово вылезает за рамку колонки на 1-3 пункта, и это становится
#     заметным дефектом верстки.
# Эффект: суммарная минимальная ширина растёт на ~13%; landscape бюджет
# 738pt пока хватает для 6-колонной таблицы реестра решений.
_TABLE_CHAR_WIDTH_PT = 6.2
# Горизонтальный padding ячейки (4pt × 2) + бордеры (~1pt) + 2pt запаса
# на округление при PDF-рендере. Каждая колонка добавляет это к
# натуральной ширине, независимо от текста.
_TABLE_CELL_CHROME_PT = 16.0
# Порог: при какой доле от portrait-ширины уже разворачиваем в landscape.
# 0.95 — оставляем небольшой буфер на округление и неточность оценки.
_PORTRAIT_USE_THRESHOLD = 0.95


def render_decisions_pdf(
    *,
    decisions: list[dict],
    project_name: str,
    mode: str,
) -> bytes:
    """Сгенерировать PDF реестра решений проекта.

    Таблица читается слева направо в естественном порядке:
    «какого уровня → что именно решается → подробности → что принято →
    какие были альтернативы». Столбцы:

    | Уровень | Что решается | Подробное описание | Принятый вариант | Альтернативы |

    * Принятый вариант — выбранное жирным + уверенность% + пояснение; при
      свободном ответе пользователя показываем его; помечаем низкую
      уверенность.
    * Альтернативы — каждая невыбранная на своей строке (название +
      уверенность% + пояснение), чтобы их было видно по отдельности, а не
      сплошным текстом.
    * Отдельные столбцы «Источник» и «Уверенность» убраны: источник не несёт
      пользы читателю, а уверенность теперь стоит рядом с самим вариантом.

    Весь документ — горизонтальный (page_orientation="landscape"): заголовок и
    таблица на одних альбомных страницах, без выноса на отдельный лист.

    Args:
        decisions: список DecisionItemView в виде dict (то, что отдаёт API).
        project_name: имя проекта для заголовка PDF.
        mode: текущий участия-режим (`autopilot`/`balanced`/...).

    Returns:
        PDF-документ как bytes.
    """
    _LEVEL_RU = {"business": "Бизнес", "architecture": "Архитектура", "detail": "Детали"}

    def _cell(text: object) -> str:
        """Экранировать спецсимволы для ячейки markdown-таблицы.

        Экранируем «|» (разделитель колонок) и «<»/«>» (чтобы текст из LLM не
        приняли за HTML), переносы строк сводим в пробел, пустое → «—».
        Внутриячеечные `<br/>` и `**` добавляются ВНЕ этой функции и потому не
        экранируются.
        """
        if text is None:
            return "—"
        cleaned = (
            str(text)
            .replace("|", "\\|")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", " ")
            .strip()
        )
        return cleaned or "—"

    # v3.5: сортировка по важности — та же, что в UI DecisionsRegistryPage:
    #   1) status=proposed (ждут ответа пользователя) сверху,
    #   2) is_low_confidence (LLM не уверена),
    #   3) уровень: business → architecture → detail,
    #   4) дата создания (свежее — выше).
    # Сохраняем стабильный порядок: пользователь, открывая PDF, видит то же
    # самое, что и на экране — не должно быть «в реестре было одно, в PDF
    # внезапно другое».
    _LEVEL_WEIGHT = {"business": 0, "architecture": 1, "detail": 2}

    def _neg_iso(s: str) -> str:
        """Для desc-сортировки строки сравниваем в reverse — берём «инверсию»
        через xor-каждого-символа; стабильный proxy для tuple-sort."""
        # ISO-8601 строки сортируются лексикографически. Чтобы получить
        # desc внутри одного tuple-ключа, инвертируем через chr(255 - ord(c)).
        return "".join(chr(255 - ord(c)) for c in s)

    sorted_decisions = sorted(
        decisions,
        key=lambda d: (
            0 if d.get("status") == "proposed" else 1,
            0 if d.get("is_low_confidence") else 1,
            _LEVEL_WEIGHT.get(str(d.get("level", "")), 3),
            _neg_iso(str(d.get("created_at") or "")),
        ),
    )

    lines: list[str] = []
    lines.append(f"# Реестр решений: {project_name}")
    lines.append("")
    lines.append(f"Режим участия: **{mode}** · Всего решений: **{len(sorted_decisions)}**")
    lines.append("")
    if not sorted_decisions:
        lines.append("_В реестре пока нет решений._")
        return render_artifact_pdf(
            markdown_content="\n".join(lines),
            title=f"Реестр решений — {project_name}",
            extract_wide_tables=False,
        )

    def _conf(option: dict) -> str | None:
        """Уверенность варианта в процентах, либо None."""
        try:
            return f"{round(float(option.get('confidence')) * 100)}%"
        except (TypeError, ValueError):
            return None

    def _is_chosen(option: dict, chosen_id: object) -> bool:
        return bool(option.get("is_chosen")) or option.get("option_id") == chosen_id

    def _option_block(option: dict, *, bold: bool) -> str:
        """Один вариант: «Название — уверенность%» + пояснение под ним."""
        label = _cell(option.get("label") or "—")
        head = f"**{label}**" if bold else label
        pct = _conf(option)
        if pct:
            head += f" — {pct}"
        desc = _cell(option.get("description"))
        return f"{head}<br/>{desc}" if desc != "—" else head

    def _chosen_cell(d: dict, alts: list[dict], chosen_id: object) -> str:
        parts: list[str] = []
        free = str(d.get("user_free_text_answer") or "").strip()
        if free:
            parts.append(f"**{_cell(free)}** _(свободный ответ)_")
        else:
            chosen = next((a for a in alts if _is_chosen(a, chosen_id)), None)
            parts.append(_option_block(chosen, bold=True) if chosen else "—")
        if d.get("is_low_confidence") and not d.get("user_verified"):
            parts.append("_(низкая уверенность)_")
        return "<br/>".join(parts)

    def _alts_cell(alts: list[dict], chosen_id: object) -> str:
        others = [a for a in alts if not _is_chosen(a, chosen_id)]
        if not others:
            return "—"
        # Каждая альтернатива — отдельным блоком (пустая строка между ними),
        # чтобы их было видно по отдельности, а не сплошным текстом.
        return "<br/><br/>".join(_option_block(a, bold=False) for a in others)

    lines.append(
        "| Уровень | Что решается | Подробное описание | Принятый вариант | Альтернативы |"
    )
    lines.append("|---|---|---|---|---|")
    for d in sorted_decisions:
        level = _LEVEL_RU.get(str(d.get("level", "")), str(d.get("level", "—")))
        title = _cell(d.get("title"))
        description = _cell(d.get("description"))
        alts = d.get("alternatives", []) or []
        chosen_id = d.get("chosen_option_id")
        lines.append(
            "| "
            + " | ".join(
                [level, title, description, _chosen_cell(d, alts, chosen_id), _alts_cell(alts, chosen_id)]
            )
            + " |"
        )

    return render_artifact_pdf(
        markdown_content="\n".join(lines),
        title=f"Реестр решений — {project_name}",
        extract_wide_tables=False,
        page_orientation="landscape",
    )


def render_artifact_pdf(
    *,
    markdown_content: str,
    title: str | None = None,
    extract_wide_tables: bool = True,
    page_orientation: str = "portrait",
) -> bytes:
    """Сконвертировать markdown артефакта в PDF и вернуть байты.

    Args:
        markdown_content: исходный markdown (из artifact.markdown_content).
        title: заголовок страницы (HTML ``<title>``), опционально.
        extract_wide_tables: выносить ли широкие (не влезающие в portrait)
            таблицы в раздел «Приложения» со ссылкой на месте. True — для
            нарративных документов (ТЗ): текст не рвётся вокруг альбомных
            таблиц. False — для документов-одной-таблицы (реестр решений),
            где таблица и есть содержимое: тогда она разворачивается inline.

    Returns:
        PDF-документ в виде bytes.

    Raises:
        PovGeneratorError: если HTML→PDF конверсия упала.
    """
    # Сначала пытаемся отрисовать ```mermaid``` блоки как PNG (через mmdc) и
    # подставить вместо них inline-<img>. Если mmdc не установлен или упал —
    # источник остаётся ```mermaid``` блоком и попадёт в PDF как preformatted
    # текст (текущее MVP-поведение).
    markdown_content = _replace_mermaid_blocks_with_images(markdown_content)

    html_body = md_lib.markdown(
        markdown_content,
        extensions=[
            "extra",          # tables, fenced_code, footnotes, attr_list
            "sane_lists",
            "toc",
            "md_in_html",     # passthrough для <div class="mermaid-pdf">
        ],
        output_format="xhtml",
    )

    # Auto-size колонок таблиц по содержимому + landscape-разворот для тех,
    # что не помещаются в portrait. Если HTML по какой-то причине не
    # парсится — функция возвращает исходник без правок, экспорт не падает.
    # page_orientation="landscape" — весь документ горизонтальный (реестр
    # решений): заголовок и широкая таблица на одних альбомных страницах, без
    # выноса. Тогда базовая ширина контента = landscape, а таблицы не
    # оборачиваются (страница и так широкая).
    is_landscape_doc = page_orientation == "landscape"
    page_width_pt = _LANDSCAPE_CONTENT_WIDTH_PT if is_landscape_doc else _PORTRAIT_CONTENT_WIDTH_PT
    html_body, landscape_used = _enhance_tables_in_html(
        html_body,
        extract_wide_tables=extract_wide_tables,
        page_content_width_pt=page_width_pt,
        wrap_wide_tables=not is_landscape_doc,
    )

    body_font = _ensure_body_font_registered()
    css = _build_base_css(
        body_font, include_landscape_page=landscape_used, default_landscape=is_landscape_doc
    )

    page_title = (title or "Artifact").replace("<", "&lt;").replace(">", "&gt;")
    # ITMO-брендинг (v3.8.4): шапка в начале и контактный блок в конце
    # каждого PDF. Заданы как inline-HTML вокруг html_body, потому что
    # xhtml2pdf не поддерживает CSS @page running-elements (то есть нельзя
    # сделать настоящие header/footer на каждой странице через CSS).
    # Поэтому это «одноразовые» блоки на первой и последней странице
    # документа — достаточно для атрибуции, не претендуют на page-header.
    header_block = (
        '<div class="doc-header">'
        "Выполнено <strong>AI Talent Hub ИТМО</strong> · "
        '<a href="https://ai.itmo.ru">ai.itmo.ru</a>'
        "</div>"
    )
    # Подвал в потоке НЕ ставим: инлайн прилипает сразу под текст, а фрейм
    # повторяется на каждой странице. Вместо этого рендерим документ без
    # подвала, а затем накладываем его у самого низа ПОСЛЕДНЕЙ страницы
    # пост-обработкой (_stamp_footer_on_last_page) — это единственный
    # надёжный способ «подвал у низа листа последней страницы».
    html_document = (
        "<!DOCTYPE html>"
        '<html lang="ru"><head>'
        '<meta charset="utf-8"/>'
        f"<title>{page_title}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        f"{header_block}"
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
    # Контактный подвал — у самого низа последней страницы (наложением).
    return _stamp_footer_on_last_page(buffer.getvalue(), body_font)


def _stamp_footer_on_last_page(pdf_bytes: bytes, body_font: str) -> bytes:
    """Наложить контактный подвал у самого низа ПОСЛЕДНЕЙ страницы.

    Документ рендерится без подвала в потоке; здесь открываем готовый PDF,
    рисуем подвал в нижнем поле последней страницы (под контентом, в зоне
    margin) и сливаем оверлей с этой страницей. Так подвал оказывается ровно
    у низа листа последней страницы, на которой есть и основной контент.

    Деградация: при любой ошибке возвращаем исходный PDF без подвала —
    экспорт не должен падать из-за брендинга.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:  # noqa: BLE001
        return pdf_bytes
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return pdf_bytes
        box = reader.pages[-1].mediabox
        overlay_pdf = _build_footer_overlay(float(box.width), float(box.height), body_font)
        overlay_page = PdfReader(io.BytesIO(overlay_pdf)).pages[0]
        # Клонируем в writer и сливаем оверлей с уже привязанной к writer
        # страницей — это надёжный путь pypdf (merge_page на странице из
        # reader помечен deprecated/«ненадёжный»).
        writer = PdfWriter(clone_from=reader)
        writer.pages[-1].merge_page(overlay_page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PDF export: не удалось наложить подвал на последнюю страницу: %s. "
            "PDF останется без контактного подвала.",
            exc,
        )
        return pdf_bytes


def _build_footer_overlay(page_width: float, page_height: float, body_font: str) -> bytes:
    """Одностраничный PDF-оверлей с контактным подвалом у низа листа.

    Размер страницы оверлея = размеру целевой страницы (portrait/landscape).
    Подвал в нижнем поле: тонкая линия + строка контактов, «Олег Шатов»
    жирным, email — синей кликабельной ссылкой.
    """
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as _canvas

    is_landscape = page_width > page_height
    side = (1.0 if is_landscape else 1.8) * cm
    left = side
    right = page_width - side
    line_y = 1.1 * cm
    text_y = 0.66 * cm
    size = 8.5
    gray = (0.40, 0.40, 0.40)     # #666
    rule = (0.69, 0.69, 0.69)     # #b0b0b0
    blue = (0.165, 0.365, 0.690)  # #2a5db0

    bold_font = (
        _PDF_FONT_NAME_BOLD
        if _PDF_FONT_NAME_BOLD in pdfmetrics.getRegisteredFontNames()
        else body_font
    )

    buf = io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=(page_width, page_height))
    c.setStrokeColorRGB(*rule)
    c.setLineWidth(0.5)
    c.line(left, line_y, right, line_y)

    cursor = left

    def seg(text: str, font: str, color: tuple[float, float, float], link: str | None = None) -> None:
        nonlocal cursor
        c.setFont(font, size)
        c.setFillColorRGB(*color)
        c.drawString(cursor, text_y, text)
        width = pdfmetrics.stringWidth(text, font, size)
        if link:
            c.linkURL(link, (cursor, text_y - 1.5, cursor + width, text_y + size), relative=0)
        cursor += width

    seg("По вопросам реализации: ", body_font, gray)
    seg("Олег Шатов", bold_font, gray)
    seg(" · ", body_font, gray)
    seg("oishatov@itmo.ru", body_font, blue, link="mailto:oishatov@itmo.ru")
    seg(" · +7 963 460-89-19", body_font, gray)

    c.showPage()
    c.save()
    return buf.getvalue()


# --- внутреннее: пред-обработка Mermaid-блоков ----------------------------


_MERMAID_FENCED_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def _replace_mermaid_blocks_with_images(markdown_text: str) -> str:
    """Найти ```mermaid``` блоки и заменить на inline-img c data-URI PNG.

    На каждый блок дёргаем ``render_mermaid_to_png`` (subprocess к ``mmdc``).
    Если рендер вернул ``None`` (mmdc не установлен / упал / отключён через
    ``POV_MERMAID_DISABLED``) — оставляем исходный markdown-блок как есть,
    чтобы в PDF попал хотя бы preformatted-исходник.
    """
    if "```mermaid" not in markdown_text:
        return markdown_text

    def _replace(match: re.Match[str]) -> str:
        source = match.group(1).strip("\n")
        if not source.strip():
            return match.group(0)
        png_bytes = render_mermaid_to_png(source)
        if png_bytes is None:
            return match.group(0)
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return (
            '\n<div class="mermaid-pdf">'
            f'<img src="data:image/png;base64,{encoded}" alt="Mermaid diagram" />'
            "</div>\n"
        )

    return _MERMAID_FENCED_RE.sub(_replace, markdown_text)


# --- внутреннее: пост-обработка таблиц (auto-width + landscape) ---------------


@dataclass(frozen=True)
class _ColumnMetrics:
    """Натуральная ширина колонки в условных «символах».

    - ``longest_word``: длина самого длинного неразрывного слова. Это нижняя
      граница ширины колонки: уже сделать нельзя, иначе слово вылетит за
      пределы ячейки.
    - ``mean_text_len``: средняя длина текста по строкам — приближение к
      «комфортной» ширине, при которой большинство строк не нуждается в
      переносе.

    Эффективный вес = max(longest_word, mean_text_len). Так колонка с одним
    длинным URL и пустыми остальными ячейками всё равно получит достаточно
    места, а колонка с равномерно длинным текстом не «съест» соседей за
    счёт одной аномальной строки.
    """

    longest_word: int
    mean_text_len: float

    @property
    def weight(self) -> float:
        return float(max(self.longest_word, self.mean_text_len, 1))


def _enhance_tables_in_html(
    html_body: str,
    *,
    extract_wide_tables: bool = True,
    page_content_width_pt: float = _PORTRAIT_CONTENT_WIDTH_PT,
    wrap_wide_tables: bool = True,
) -> tuple[str, bool]:
    """Пост-обработка HTML перед отдачей в xhtml2pdf — за один разбор дерева.

    Делает три вещи:
      1. auto-width колонок таблиц по содержимому;
      2. широкие таблицы (не влезающие в portrait):
         - ``extract_wide_tables=False`` — разворот в landscape НА МЕСТЕ
           (для документов-одной-таблицы, например реестра решений);
         - ``extract_wide_tables=True`` — ВЫНОС в раздел «Приложения» в
           конце документа, а на месте остаётся кликабельная ссылка. Так
           нарративный текст не рвётся полупустыми страницами вокруг
           альбомной таблицы;
      3. кликабельное оглавление по заголовкам разделов + якоря.

    Возвращает ``(html, landscape_used)``. ``landscape_used`` управляет
    подключением @page landscape-rule в CSS.

    Если HTML не парсится — возвращаем исходник без правок: экспорт не
    должен падать из-за косметики.
    """
    try:
        # Markdown даёт фрагмент; оборачиваем в единственный root.
        root = ET.fromstring(f"<root>{html_body}</root>")
    except ET.ParseError as exc:
        logger.warning(
            "PDF export: не удалось распарсить HTML для пост-обработки: %s. "
            "Документ пойдёт без autosize таблиц и оглавления.",
            exc,
        )
        return html_body, False

    # parent_map нужен для навигации child→parent (у ET её нет).
    parent_map = {child: parent for parent in root.iter() for child in parent}

    landscape_used, appendix = _process_tables(
        root,
        parent_map,
        extract_wide_tables=extract_wide_tables,
        page_content_width_pt=page_content_width_pt,
        wrap_wide_tables=wrap_wide_tables,
    )
    if appendix:
        _append_table_appendix(root, appendix)
        landscape_used = True

    # Оглавление строим ПОСЛЕ выноса приложений — чтобы их заголовки тоже
    # попали в содержание и были кликабельны.
    _inject_toc_and_anchors(root)

    serialized = _serialize_root(root)
    # Финальная подмена placeholder-div'ов (см. _wrap_in_landscape) на
    # reportlab-теги <pdf:nextpage> — в самом конце, на строке: ET не умеет
    # создавать элементы с двоеточием в имени без xmlns-объявления.
    serialized = _PLACEHOLDER_PAGE_OPEN.sub(r'<pdf:nextpage name="\1" />', serialized)
    return serialized, landscape_used


def _serialize_root(root: ET.Element) -> str:
    """Сериализовать содержимое root в HTML-фрагмент (сам тег root не отдаём)."""
    pieces: list[str] = []
    if root.text:
        pieces.append(root.text)
    for child in root:
        # method="xml" гарантирует self-closed <col/> — валидный XHTML.
        pieces.append(ET.tostring(child, encoding="unicode", method="xml"))
        if child.tail:
            pieces.append(child.tail)
    return "".join(pieces)


def _process_tables(
    root: ET.Element,
    parent_map: dict,
    *,
    extract_wide_tables: bool,
    page_content_width_pt: float = _PORTRAIT_CONTENT_WIDTH_PT,
    wrap_wide_tables: bool = True,
) -> tuple[bool, list[tuple[str, ET.Element]]]:
    """Авто-ширина колонок + обработка широких таблиц.

    Узкие таблицы получают colgroup и остаются на месте. Широкие — либо
    разворачиваются в landscape на месте (``extract_wide_tables=False``),
    либо выносятся в приложение (возвращаются списком, на их месте —
    ссылка-плейсхолдер).

    Возвращает ``(landscape_used_inline, appendix_items)``, где
    ``appendix_items`` — список ``(подпись, элемент-таблица)``.
    """
    # Снимок таблиц ДО мутаций: мутация структуры во время обхода ET
    # приводит к пропуску элементов.
    tables = list(root.iter("table"))
    if not tables:
        return False, []

    # Подписи приложений — по ближайшему заголовку секции; считаем по
    # исходному порядку блоков ДО мутаций.
    caption_map = _table_section_captions(root, tables)

    landscape_used = False
    appendix: list[tuple[str, ET.Element]] = []

    for table in tables:
        metrics = _compute_column_metrics(table)
        if not metrics:
            continue

        if not wrap_wide_tables:
            # Документ уже горизонтальный (landscape): не оборачиваем и не
            # выносим — фиксируем ширины колонок по ширине самой страницы.
            _inject_colgroup(table, metrics, available_pt=page_content_width_pt)
            continue

        natural_pt = _estimate_table_width_pt(metrics)
        is_wide = natural_pt > page_content_width_pt * _PORTRAIT_USE_THRESHOLD

        if not is_wide:
            _inject_colgroup(table, metrics, available_pt=page_content_width_pt)
            continue

        if not extract_wide_tables:
            # Документ-одна-таблица: разворачиваем на месте.
            _inject_colgroup(table, metrics, available_pt=_LANDSCAPE_CONTENT_WIDTH_PT)
            _wrap_in_landscape(table, parent_map)
            landscape_used = True
            continue

        # Нарративный документ: выносим таблицу в приложение, на месте —
        # кликабельная ссылка на якорь приложения.
        index = len(appendix) + 1
        caption = caption_map.get(table) or f"Таблица {index}"
        _replace_table_with_reference(table, parent_map, index=index, caption=caption)
        appendix.append((caption, table))

    return landscape_used, appendix


def _heading_text(element: ET.Element) -> str:
    """Полный текст заголовка (с учётом вложенных ссылок и их хвостов)."""
    return "".join(element.itertext()).strip()


_HEADING_TAGS = ("h1", "h2", "h3", "h4")


def _table_section_captions(
    root: ET.Element, tables: list[ET.Element]
) -> dict[ET.Element, str]:
    """Сопоставить каждой таблице ближайший предшествующий заголовок секции."""
    table_set = set(tables)
    captions: dict[ET.Element, str] = {}
    current_heading = ""
    for element in root.iter():
        if element.tag in _HEADING_TAGS:
            current_heading = _heading_text(element)
        elif element.tag == "table" and element in table_set:
            captions[element] = current_heading
    return captions


def _replace_table_with_reference(
    table: ET.Element,
    parent_map: dict,
    *,
    index: int,
    caption: str,
) -> None:
    """Заменить таблицу на месте кликабельной ссылкой на её приложение."""
    parent = parent_map.get(table)
    if parent is None:
        return
    try:
        idx = list(parent).index(table)
    except ValueError:
        return
    ref = ET.Element("p", {"class": "table-ref"})
    ref.text = "См. "
    link = ET.SubElement(ref, "a", {"href": f"#appendix-{index}"})
    link.text = f"Приложение {index}. {caption}"
    parent.remove(table)
    parent.insert(idx, ref)
    parent_map[ref] = parent


def _append_table_appendix(
    root: ET.Element, appendix: list[tuple[str, ET.Element]]
) -> None:
    """Добавить раздел «Приложения» с вынесенными широкими таблицами.

    Заголовок «Приложения» и первая таблица идут на ОДНОЙ landscape-странице:
    переход в альбомную ориентацию ставим ПЕРЕД заголовком, иначе он остаётся
    на portrait-листе и между ним и приложениями возникает полупустая страница.
    Каждая следующая таблица — на собственной landscape-странице, под заголовком
    с якорем ``appendix-N`` (на него ссылается плейсхолдер на месте таблицы).
    """
    # Переход на landscape ДО заголовка раздела.
    first_break = ET.SubElement(root, "div")
    first_break.set("data-pov-pdf-page", "landscape_page")
    heading = ET.SubElement(root, "h2")
    heading.text = "Приложения"
    for index, (caption, table) in enumerate(appendix, start=1):
        if index > 1:
            # Каждая следующая таблица — на новой landscape-странице.
            landscape_break = ET.SubElement(root, "div")
            landscape_break.set("data-pov-pdf-page", "landscape_page")
        item_heading = ET.SubElement(root, "h3")
        anchor = ET.SubElement(item_heading, "a", {"name": f"appendix-{index}"})
        # Текст — ВНУТРИ закрытого якоря (а не в .tail): пустой <a/> ломает
        # парсер и красит текст ссылочным цветом.
        anchor.text = f"Приложение {index}. {caption}"
        metrics = _compute_column_metrics(table)
        if metrics:
            _inject_colgroup(table, metrics, available_pt=_LANDSCAPE_CONTENT_WIDTH_PT)
        root.append(table)
    # Намеренно НЕ возвращаемся на portrait после приложений: лишний
    # <pdf:nextpage name="body_page"> создавал пустую последнюю страницу. Подвал
    # теперь рендерится фрейм-подвалом на каждой странице, инлайн-блока нет.


def _inject_toc_and_anchors(root: ET.Element) -> None:
    """Проставить якоря на заголовки h2/h3 и вставить кликабельное оглавление.

    Якорь — ``<a name="sec-N">`` в начале заголовка; на него ссылается
    оглавление через ``<a href="#sec-N">``. xhtml2pdf резолвит внутренние
    ссылки именно по ``name``-якорям. Оглавление вставляется сразу после
    заголовка документа (h1). Если значимых разделов меньше двух —
    оглавление не добавляем.
    """
    headings = [el for el in root.iter() if el.tag in ("h2", "h3")]
    entries: list[tuple[str, str, str]] = []
    seq = 0
    for heading in headings:
        text = _heading_text(heading)
        if not text:
            continue
        existing = heading.find("a")
        if existing is not None and existing.get("name"):
            # Заголовок приложения уже несёт именованный якорь — переиспользуем
            # его (на него же ссылается плейсхолдер на месте таблицы).
            anchor_id = existing.get("name") or ""
        else:
            seq += 1
            anchor_id = f"sec-{seq}"
            # Оборачиваем ВСЁ содержимое заголовка в ЗАКРЫТЫЙ <a name>.
            # КРИТИЧНО: пустой самозакрывающийся <a/> html5lib (парсер
            # xhtml2pdf) трактует как НЕзакрытый тег (a — не void), и весь
            # последующий текст красится в цвет ссылки — это и был «синий PDF».
            anchor = ET.Element("a", {"name": anchor_id})
            anchor.text = heading.text
            heading.text = None
            for child in list(heading):
                heading.remove(child)
                anchor.append(child)
            heading.append(anchor)
        heading.set("id", anchor_id)
        entries.append((heading.tag, anchor_id, text))

    if len(entries) < 2:
        return

    toc = ET.Element("div", {"class": "doc-toc"})
    toc_title = ET.SubElement(toc, "p", {"class": "doc-toc-title"})
    toc_title.text = "Содержание"
    for tag, anchor_id, text in entries:
        item = ET.SubElement(toc, "p", {"class": f"toc-{tag}"})
        link = ET.SubElement(item, "a", {"href": f"#{anchor_id}"})
        link.text = text

    insert_at = 0
    for position, child in enumerate(list(root)):
        if child.tag == "h1":
            insert_at = position + 1
            break
    root.insert(insert_at, toc)
    # Явный разделитель ПОД оглавлением (отдельным элементом, см. CSS .doc-toc-rule)
    # — чтобы начало документа визуально не сливалось с содержанием.
    root.insert(insert_at + 1, ET.Element("hr", {"class": "doc-toc-rule"}))


def _compute_column_metrics(table: ET.Element) -> list[_ColumnMetrics]:
    """Собрать натуральные ширины колонок по содержимому ячеек."""
    # Собираем строки: учитываем <tr> и в <thead>, и в <tbody>, и прямо
    # внутри <table> (Markdown extra кладёт их под thead/tbody).
    rows: list[list[ET.Element]] = []
    for tr in table.iter("tr"):
        cells = [cell for cell in tr if cell.tag in ("td", "th")]
        if cells:
            rows.append(cells)

    if not rows:
        return []

    # Если строки разной ширины (битый markdown / colspan) — берём максимум.
    num_cols = max(len(r) for r in rows)
    if num_cols == 0:
        return []

    per_col_words: list[list[int]] = [[] for _ in range(num_cols)]
    per_col_lengths: list[list[int]] = [[] for _ in range(num_cols)]

    for row in rows:
        for col_idx, cell in enumerate(row):
            text = "".join(cell.itertext())
            # Нормализуем пробелы — markdown добавляет \n внутри ячеек.
            text = " ".join(text.split())
            per_col_lengths[col_idx].append(len(text))
            longest = max((len(w) for w in text.split()), default=0)
            per_col_words[col_idx].append(longest)

    metrics: list[_ColumnMetrics] = []
    for col_idx in range(num_cols):
        lengths = per_col_lengths[col_idx] or [0]
        words = per_col_words[col_idx] or [0]
        mean_len = sum(lengths) / len(lengths)
        longest_word = max(words)
        metrics.append(_ColumnMetrics(longest_word=longest_word, mean_text_len=mean_len))
    return metrics


def _inject_colgroup(
    table: ET.Element,
    metrics: list[_ColumnMetrics],
    *,
    available_pt: float = _PORTRAIT_CONTENT_WIDTH_PT,
) -> None:
    """Зафиксировать пропорциональные ширины колонок для xhtml2pdf.

    КРИТИЧНО ПРО xhtml2pdf. Движок **игнорирует** ``<colgroup>``/``<col>``
    — даже с ``table-layout: fixed`` и ``<col style="width: X%">``.
    Реальный путь, по которому ширины попадают в reportlab Table:
    атрибут ``width`` на ячейках ``<td>`` / ``<th>`` (см.
    ``xhtml2pdf/tables.py:345``: ``width = c.frag.width or self.attr.width``).

    Соответственно фиксируем ширины **на header-ячейках первой строки**:
    xhtml2pdf пройдёт по ним и зафиксирует ``tdata.colw`` для всей
    таблицы. ``<colgroup>`` оставляем тоже — он корректно
    интерпретируется обычными HTML-просмотрщиками.

    АЛГОРИТМ РАСПРЕДЕЛЕНИЯ ШИРИН (v3.8.1, исправляет overflow).

    Раньше использовался %-floor 5% от ширины страницы. Это плохо
    работало для landscape-таблиц с очень длинными заголовками или
    короткими но непереносимыми словами: «Архитектура» (11 символов) ×
    5.5pt/символ = 60pt минимум; 5% от 738pt landscape = 37pt → текст
    вылезал за границы колонки.

    Новый алгоритм:
      1) Для каждой колонки считаем минимальную ширину в pt —
         ``longest_word * char_width + chrome``. Это та ширина, ниже
         которой текст ГАРАНТИРОВАННО переполнится.
      2) Если суммарный минимум превышает доступную ширину — что-то не
         так со страницей (вызывающий должен был развернуть в landscape).
         В этом случае всё равно распределяем минимумы пропорционально —
         лучше частично сжатый текст, чем катастрофа layout.
      3) Излишек (available − sum(min)) распределяем пропорционально
         весам колонок: колонки с большим контентом получают больше
         «бонусной» ширины поверх своего минимума.
      4) Конвертируем pt → % от доступной ширины и кладём в width="X%"
         на header-ячейки.
    """
    if not metrics:
        return

    # 1) Минимумы по pt: longest_word + chrome — нижняя граница, ниже
    # которой текст обрезается прямо посреди слова.
    min_widths_pt = [
        m.longest_word * _TABLE_CHAR_WIDTH_PT + _TABLE_CELL_CHROME_PT
        for m in metrics
    ]
    sum_min = sum(min_widths_pt)

    # 2) Бюджет на распределение бонусов.
    bonus_pool = max(0.0, available_pt - sum_min)

    # 3) Распределяем бонусы пропорционально весам.
    weights = [m.weight for m in metrics]
    total_weight = sum(weights) or 1.0
    widths_pt = [
        base + bonus_pool * (w / total_weight)
        for base, w in zip(min_widths_pt, weights)
    ]

    # Если минимумы не лезут — нормализуем целиком (sum=available).
    # Это аварийный режим: layout всё равно будет тесным, но без
    # переполнения за границы страницы.
    total_w = sum(widths_pt)
    if total_w > available_pt:
        scale = available_pt / total_w
        widths_pt = [w * scale for w in widths_pt]

    # 4) Конвертируем в % от available_pt — xhtml2pdf принимает и %,
    # и pt, но % лучше переживают изменение размера страницы.
    percents = [(w / available_pt) * 100.0 for w in widths_pt]

    # <colgroup> для семантической корректности HTML.
    for existing in list(table.findall("colgroup")):
        table.remove(existing)
    colgroup = ET.Element("colgroup")
    for pct in percents:
        col = ET.SubElement(colgroup, "col")
        col.set("style", f"width: {pct:.2f}%;")
    table.insert(0, colgroup)

    # ГЛАВНОЕ: ставим width="X%" как HTML-атрибут на ячейки первой
    # строки. Именно это xhtml2pdf будет читать.
    first_row_cells = _first_row_cells(table)
    if first_row_cells and len(first_row_cells) == len(percents):
        for cell, pct in zip(first_row_cells, percents):
            cell.set("width", f"{pct:.2f}%")


def _first_row_cells(table: ET.Element) -> list[ET.Element]:
    """Найти ячейки первой строки таблицы (thead/tbody/прямо в table).

    Возвращает первую найденную <tr> с непустым списком <th>/<td>.
    Если таблица битая (нет строк) — пустой список.
    """
    for tr in table.iter("tr"):
        cells = [cell for cell in tr if cell.tag in ("td", "th")]
        if cells:
            return cells
    return []


def _estimate_table_width_pt(metrics: list[_ColumnMetrics]) -> float:
    """Оценка натуральной ширины таблицы в pt.

    Натуральная = ширина, при которой текст не нуждается в переносе. Если
    она превышает доступную в portrait — таблицу разворачиваем.
    """
    if not metrics:
        return 0.0
    chars_total = sum(m.weight for m in metrics)
    text_width = chars_total * _TABLE_CHAR_WIDTH_PT
    chrome_width = len(metrics) * _TABLE_CELL_CHROME_PT
    return text_width + chrome_width


def _wrap_in_landscape(table: ET.Element, parent_map: dict) -> None:
    """Перевести таблицу на landscape-страницу через placeholder'ы.

    Стратегия: ставим перед таблицей и (если есть последующий контент)
    после — placeholder-div'ы. На этапе финальной сериализации они
    превращаются в reportlab-теги ``<pdf:nextpage name="..." />``,
    которые вставляют PageBreak + переключают активный page template.

    Альтернатива — CSS-свойство ``page: name`` или class с @page-rule —
    в xhtml2pdf отрабатывает непредсказуемо (часто игнорируется и обе
    страницы выходят portrait). Через нативный <pdf:nextpage> работает
    надёжно.

    Почему не вставляем reportlab-тег напрямую: ET не умеет создавать
    XML-элементы с двоеточием в имени без полноценного xmlns-объявления,
    а xhtml2pdf-парсер ищет именно литерал ``pdf:nextpage``. Подмена
    placeholder'ов на сериализованной строке — самый надёжный способ.
    """
    parent = parent_map.get(table)
    if parent is None:
        # Корень не оборачиваем — это привело бы к рекурсии в xhtml2pdf.
        return
    siblings = list(parent)
    try:
        idx = siblings.index(table)
    except ValueError:
        return

    open_placeholder = ET.Element(
        "div",
        {"data-pov-pdf-page": "landscape_page"},
    )
    parent.insert(idx, open_placeholder)
    parent_map[open_placeholder] = parent

    # Индекс таблицы после вставки open_placeholder сдвинулся на +1.
    table_idx = idx + 1
    new_siblings = list(parent)
    has_next_content = (table_idx + 1) < len(new_siblings)
    if has_next_content:
        close_placeholder = ET.Element(
            "div",
            {"data-pov-pdf-page": "body_page"},
        )
        parent.insert(table_idx + 1, close_placeholder)
        parent_map[close_placeholder] = parent


# Regex для подмены placeholder-div'ов на reportlab-теги <pdf:nextpage>.
# ET сериализует атрибуты в виде data-pov-pdf-page="value" (двойные кавычки)
# и закрывает div самозакрывающимся слэшем или парой <div ...></div>.
# Покрываем оба варианта.
_PLACEHOLDER_PAGE_OPEN = re.compile(
    r'<div\s+data-pov-pdf-page="([^"]+)"\s*(?:/>|>\s*</div>)'
)


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


def _build_base_css(
    body_font: str, *, include_landscape_page: bool = False, default_landscape: bool = False
) -> str:
    """Базовый CSS для PDF.

    Важно: ``font-family`` задаётся на ``body`` И на всех ключевых
    элементах (h1..h4, p, li, table, th, td, blockquote). xhtml2pdf
    не всегда наследует font-family корректно через каскад, поэтому
    задаём явно.

    Mono-шрифт (code / pre) — оставляем Courier как core PDF font;
    латиница в коде покрыта, а если в коде встречается кириллица —
    переключаем на body_font тоже.

    Args:
        include_landscape_page: добавить ли именованную @page rule для
            landscape-блоков. Подаём только когда в HTML действительно есть
            широкая таблица, обёрнутая в ``.pdf-landscape-page`` — иначе
            xhtml2pdf может зарезервировать пустую страницу в конце.

    Заметки про таблицы:
        - ``table-layout: fixed`` — обязательно, чтобы xhtml2pdf уважал
          ширины из ``<col style="width: X%">``. Без fixed движок
          переключается в auto-layout и игнорирует наш ``<colgroup>``,
          возвращаясь к равному делению.
        - ``word-wrap: break-word`` на ``th/td`` — страховка от ячейки с
          одним очень длинным URL: даже в landscape она не должна
          растягивать колонку за границы страницы.
    """
    # body_page и landscape_page — именованные @page-rules. На них ссылается
    # <pdf:nextpage name="..." />, вставляемый при wrap-в-landscape (см.
    # _wrap_in_landscape). landscape-rule добавляется только когда в
    # документе реально есть широкая таблица — чтобы не плодить лишних
    # @page-templates без необходимости.
    landscape_rules = (
        """
        @page landscape_page {
            size: A4 landscape;
            margin: 1.4cm 1.0cm;
        }
        """
        if include_landscape_page
        else ""
    )
    # Дефолтная (безымянная) @page. Для реестра решений — landscape, чтобы
    # заголовок и широкая таблица были на одних горизонтальных страницах.
    default_page = (
        "size: A4 landscape; margin: 1.4cm 1.0cm;"
        if default_landscape
        else "size: A4 portrait; margin: 2cm 1.8cm;"
    )
    return f"""
        @page body_page {{
            size: A4 portrait;
            margin: 2cm 1.8cm;
        }}
        @page {{
            {default_page}
        }}
        {landscape_rules}
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
        /* По умолчанию ссылки/якоря НЕ красим: иначе xhtml2pdf даёт им
           синий цвет ссылки. Якоря заголовков (цель оглавления) должны
           выглядеть как обычный текст. Реальные внешние ссылки в шапке/
           подвале подкрашиваются явно ниже. */
        a {{ color: inherit; text-decoration: none; }}
        table {{
            border-collapse: collapse;
            table-layout: fixed;
            width: 100%;
            margin: 6pt 0 10pt 0;
            font-size: 9.5pt;
            /* Узкие таблицы не рвём посреди — лучше перенести целиком. */
            page-break-inside: avoid;
        }}
        tr {{ page-break-inside: avoid; }}
        th, td {{
            font-family: {body_font};
            border: 0.5pt solid #999;
            padding: 4pt 6pt;
            vertical-align: top;
            text-align: left;
            word-wrap: break-word;
            /* v3.8.3: НИКАКОГО `-pdf-word-wrap: CJK`. Эта опция
               включает в reportlab режим, при котором текст
               разрывается посреди буквы при первом же намёке на
               нехватку места — даже если слово целиком прекрасно
               помещается с обычным пробельным переносом. На практике
               это рисовало «Уверенност / ь», «лин / ии» и подобный
               мусор в каждой второй ячейке. Положимся на
               `word-wrap: break-word` + правильно посчитанные мин-
               ширины колонок (longest_word × char_width + chrome). */
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
        .mermaid-pdf {{
            margin: 8pt 0;
            text-align: center;
        }}
        .mermaid-pdf img {{
            max-width: 100%;
            height: auto;
        }}
        /* ITMO-брендинг — шапка/подвал документа. Намеренно ненавязчивые:
           серый цвет, мелкий шрифт, отделены тонкой линией. */
        .doc-header {{
            font-family: {body_font};
            font-size: 8.5pt;
            color: #666;
            border-bottom: 0.5pt solid #b0b0b0;
            padding-bottom: 4pt;
            margin: 0 0 12pt 0;
        }}
        .doc-header a {{ color: #2a5db0; text-decoration: none; }}
        .doc-footer {{
            font-family: {body_font};
            font-size: 8.5pt;
            color: #666;
            border-top: 0.5pt solid #b0b0b0;
            padding-top: 4pt;
            margin: 16pt 0 0 0;
            /* не разрывать подвал между страницами */
            page-break-inside: avoid;
        }}
        .doc-footer a {{ color: #2a5db0; text-decoration: none; }}
        /* Оглавление — без коробки и без синих ссылок: монохромно, в стиле
           самого документа. Разделитель между TOC и телом — ОТДЕЛЬНЫЙ <hr>
           (не border-bottom самого блока: xhtml2pdf дублирует border контейнера
           под каждым дочерним <p>, и оглавление превращается в «таблицу»). */
        .doc-toc {{
            font-family: {body_font};
            margin: 0 0 6pt 0;
            padding: 0;
            page-break-inside: avoid;
            page-break-after: avoid;
        }}
        .doc-toc-rule {{
            background-color: #bbb;
            height: 0.75pt;
            border: none;
            margin: 4pt 0 18pt 0;
            page-break-after: avoid;
        }}
        .doc-toc-title {{
            font-family: {body_font};
            font-weight: bold;
            font-size: 13pt;
            margin: 0 0 6pt 0;
        }}
        /* Плотные строки: глобальный line-height 1.4 даёт «воздушный» список,
           для оглавления он лишний. margin-left для h3 задаём с повышенной
           специфичностью (`.doc-toc p.toc-h3`), иначе короткая запись margin в
           `.doc-toc p` обнуляла бы левый отступ и оглавление было бы плоским. */
        .doc-toc p {{ margin: 0; padding: 0.6pt 0; line-height: 1.05; }}
        .doc-toc p.toc-h2 {{ font-size: 10pt; margin-left: 0; }}
        .doc-toc p.toc-h3 {{ font-size: 9.5pt; color: #555; margin-left: 22pt; }}
        .doc-toc a {{ color: #1c1c1c; text-decoration: none; }}
        /* Ссылка на месте вынесенной в приложение широкой таблицы. */
        .table-ref {{
            font-family: {body_font};
            font-style: italic;
            color: #444;
            margin: 6pt 0;
        }}
        .table-ref a {{ color: #1c1c1c; text-decoration: none; }}
    """
