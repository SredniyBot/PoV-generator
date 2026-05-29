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
    """Сгенерировать PDF реестра решений проекта (v3.8.1).

    Решения отдаются в виде одной широкой таблицы со столбцами:
    Уровень · Описание · Решение · Альтернативы · Источник · Уверенность.

    v3.8: «Выбрано» (chosen_option_label) заменён на «Описание»
    (decision.description). chosen-label дублировался с альтернативами
    (выбранный помечен ✓), а полнота описания была недоступна без UI.

    v3.8.1: убран столбец «Статус» — для autopilot он всегда
    «Принят дефолт», в balanced/control/expert он редко меняется и
    место съедает. Перепорядочены так, чтобы при чтении сверху вниз
    шла естественная логика: «какого уровня → о чём → суть → варианты
    → откуда → уверенность».

    Реализация делегирует в `render_artifact_pdf` — markdown с большой
    таблицей попадёт через тот же auto-width + landscape pipeline.

    Args:
        decisions: список DecisionItemView в виде dict (то, что отдаёт API).
        project_name: имя проекта для заголовка PDF.
        mode: текущий участия-режим (`autopilot`/`balanced`/...).

    Returns:
        PDF-документ как bytes.
    """
    _LEVEL_RU = {"business": "Бизнес", "architecture": "Архитектура", "detail": "Детали"}
    _SOURCE_RU = {
        # v3.6 ребрендинг (v3.7: phase_gap удалён).
        "pre_flight": "выявлено",     # task-level identification
        "emergent": "извлечено",      # post-artifact extraction
        "reactive_validation": "валидация",
        "user_manual": "вручную",
    }

    def _cell(text: str) -> str:
        """Экранируем «pipe» и нормализуем переносы для markdown-таблицы."""
        if text is None:
            return "—"
        return str(text).replace("|", "\\|").replace("\n", " ")

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
        )

    lines.append("| Уровень | Описание | Решение | Альтернативы | Источник | Уверенность |")
    lines.append("|---|---|---|---|---|---|")
    for d in sorted_decisions:
        level = _LEVEL_RU.get(str(d.get("level", "")), str(d.get("level", "—")))
        title = str(d.get("title") or "").strip() or "—"
        description_raw = str(d.get("description") or "").strip() or "—"
        # confidence: предпочитаем chosen-alt's confidence, fallback на overall
        chosen_id = d.get("chosen_option_id")
        chosen_alt_conf = None
        alt_summaries: list[str] = []
        for alt in d.get("alternatives", []) or []:
            alt_conf = alt.get("confidence")
            tag = f"{alt.get('label', '')}"
            if alt_conf is not None:
                tag += f" ({round(float(alt_conf) * 100)}%)"
            if alt.get("option_id") == chosen_id:
                chosen_alt_conf = alt_conf
                # Помечаем выбранный вариант жирным в начале списка.
                # Markdown-bold надёжно работает в xhtml2pdf c
                # зарегистрированным bold-шрифтом; Unicode-символы
                # вроде ✓ ломаются если в шрифте нет glyph'а.
                alt_summaries.insert(0, f"**{tag}**")
            else:
                alt_summaries.append(tag)
        conf_value = chosen_alt_conf if chosen_alt_conf is not None else d.get("confidence")
        try:
            conf_pct = f"{round(float(conf_value) * 100)}%"
        except (TypeError, ValueError):
            conf_pct = "—"
        if d.get("is_low_confidence"):
            conf_pct += " ⚠"
        alts_text = "; ".join(alt_summaries) if alt_summaries else "—"
        source = _SOURCE_RU.get(str(d.get("source", "")), str(d.get("source", "—")))
        # v3.8.1 порядок: Уровень · Описание · Решение · Альтернативы · Источник · Уверенность
        lines.append(
            "| "
            + " | ".join(
                _cell(x)
                for x in (level, description_raw, title, alts_text, source, conf_pct)
            )
            + " |"
        )

    return render_artifact_pdf(
        markdown_content="\n".join(lines),
        title=f"Реестр решений — {project_name}",
    )


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

    # Auto-size колонок таблиц по содержимому + landscape-разворот для тех,
    # что не помещаются в portrait. Если HTML по какой-то причине не
    # парсится — функция возвращает исходник без правок, экспорт не падает.
    html_body, landscape_used = _enhance_tables_in_html(html_body)

    body_font = _ensure_body_font_registered()
    css = _build_base_css(body_font, include_landscape_page=landscape_used)

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
    footer_block = (
        '<div class="doc-footer">'
        "По вопросам реализации: <strong>Олег Шатов</strong> · "
        '<a href="mailto:oishatov@itmo.ru">oishatov@itmo.ru</a> · '
        "+7 963 460-89-19"
        "</div>"
    )
    html_document = (
        "<!DOCTYPE html>"
        '<html lang="ru"><head>'
        '<meta charset="utf-8"/>'
        f"<title>{page_title}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        f"{header_block}"
        f"{html_body}"
        f"{footer_block}"
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


def _enhance_tables_in_html(html_body: str) -> tuple[str, bool]:
    """Пост-обработка HTML: auto-width колонок + landscape для широких таблиц.

    Возвращает ``(modified_html, landscape_used)``. ``landscape_used`` нужен,
    чтобы CSS выдавал именованную @page-rule только когда она реально
    используется — иначе xhtml2pdf может зарезервировать пустую страницу.

    Если HTML не парсится (markdown с output_format="xhtml" в норме даёт
    валидный XML, но рисковать экспортом не стоит) — возвращаем исходник
    без правок.
    """
    try:
        # Markdown даёт фрагмент; оборачиваем в единственный root.
        root = ET.fromstring(f"<root>{html_body}</root>")
    except ET.ParseError as exc:
        logger.warning(
            "PDF export: не удалось распарсить HTML для autosize таблиц: %s. "
            "Таблицы пойдут с дефолтными равными колонками.",
            exc,
        )
        return html_body, False

    landscape_used = False

    # Снимок таблиц ДО мутаций. ET.iter — генератор; мутация структуры
    # параллельно с обходом приводит к пропуску элементов.
    tables = list(root.iter("table"))
    if not tables:
        return html_body, False

    # parent_map нужен для wrap-в-div, потому что у ET нет навигации child→parent.
    parent_map = {child: parent for parent in root.iter() for child in parent}

    for table in tables:
        metrics = _compute_column_metrics(table)
        if not metrics:
            continue
        # v3.8.1: сначала определяем, помещается ли таблица в portrait —
        # это нужно знать, чтобы передать правильную available_pt в
        # _inject_colgroup (иначе бонус-пул считается от 493pt portrait,
        # но фактически таблица будет на 738pt landscape — overflow).
        natural_pt = _estimate_table_width_pt(metrics)
        is_landscape = natural_pt > _PORTRAIT_CONTENT_WIDTH_PT * _PORTRAIT_USE_THRESHOLD
        available_pt = (
            _LANDSCAPE_CONTENT_WIDTH_PT if is_landscape else _PORTRAIT_CONTENT_WIDTH_PT
        )
        _inject_colgroup(table, metrics, available_pt=available_pt)
        if is_landscape:
            _wrap_in_landscape(table, parent_map)
            landscape_used = True

    # Сериализуем содержимое root обратно в HTML-фрагмент. Сам тег <root>
    # не отдаём наружу — он был только обёрткой для парсера.
    pieces: list[str] = []
    if root.text:
        pieces.append(root.text)
    for child in root:
        # method="xml" гарантирует self-closed <col/> — это валидный XHTML и
        # xhtml2pdf корректно разбирает оба варианта (с / без слеша).
        pieces.append(ET.tostring(child, encoding="unicode", method="xml"))
        if child.tail:
            pieces.append(child.tail)
    serialized = "".join(pieces)

    # Финальная подмена placeholder-div'ов (см. _wrap_in_landscape) на
    # настоящие reportlab-теги <pdf:nextpage>. CSS-переключение @page
    # через свойство `page: name` в xhtml2pdf работает капризно;
    # <pdf:nextpage name="..." /> — официально поддерживаемый способ
    # сменить page template посередине документа (см. tags.py:
    # pisaTagPDFNEXTPAGE → NextPageTemplate(name) + PageBreak()).
    serialized = _PLACEHOLDER_PAGE_OPEN.sub(
        r'<pdf:nextpage name="\1" />', serialized
    )
    return serialized, landscape_used


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


def _build_base_css(body_font: str, *, include_landscape_page: bool = False) -> str:
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
    return f"""
        @page body_page {{
            size: A4 portrait;
            margin: 2cm 1.8cm;
        }}
        @page {{
            size: A4 portrait;
            margin: 2cm 1.8cm;
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
        table {{
            border-collapse: collapse;
            table-layout: fixed;
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
            /* page-break-inside на блоке-подвале: не хочется, чтобы
               «Олег Шатов» и его email уехали на разные страницы. */
            page-break-inside: avoid;
        }}
        .doc-footer a {{ color: #2a5db0; text-decoration: none; }}
    """
