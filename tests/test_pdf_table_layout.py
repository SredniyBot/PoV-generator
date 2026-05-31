"""Тесты для autosize колонок и landscape-разворота широких таблиц в PDF.

Покрывают чистую функцию `_enhance_tables_in_html`, без рендеринга в
reportlab — это даёт детерминированные ассерты на структуру HTML, не
завязанные на байтовое представление PDF.

Конец цепочки — что итоговый PDF не падает на широкой таблице — покрыт
существующим `test_pdf_export.py`.
"""

from __future__ import annotations

import re

import markdown as md_lib

from pov_generator.application.pdf_export import (
    _LANDSCAPE_CONTENT_WIDTH_PT,
    _PORTRAIT_CONTENT_WIDTH_PT,
    _ColumnMetrics,
    _compute_column_metrics,
    _enhance_tables_in_html,
    _estimate_table_width_pt,
)


def _md_to_html(markdown_src: str) -> str:
    """Преобразовать markdown в тот же XHTML-фрагмент, что и в pdf_export."""
    return md_lib.markdown(
        markdown_src,
        extensions=["extra", "sane_lists", "toc"],
        output_format="xhtml",
    )


def _extract_widths(html: str) -> list[float]:
    """Вытащить процентные ширины из ``<col style="width: X%;">``."""
    return [float(m) for m in re.findall(r'width:\s*([\d.]+)%', html)]


# ---------------------------------------------------------------------------
# Базовое поведение: узкая таблица → portrait + colgroup
# ---------------------------------------------------------------------------


def test_narrow_table_stays_portrait_but_gets_colgroup() -> None:
    """Узкая таблица не уходит в landscape, но получает proportional widths.

    Поведение «было» (равные доли) больше не применяется — colgroup
    вставляется всегда, даже когда таблица помещается на portrait.
    """
    md = (
        "| Поле | Значение |\n"
        "|---|---|\n"
        "| Имя | Альфа |\n"
        "| Тип | Запрос |\n"
    )
    html = _md_to_html(md)
    out, landscape = _enhance_tables_in_html(html)

    assert landscape is False
    assert "<colgroup>" in out
    widths = _extract_widths(out)
    assert len(widths) == 2
    assert abs(sum(widths) - 100.0) < 1.0  # сумма ≈ 100%


def test_wide_risk_register_goes_landscape() -> None:
    """Реалистичный реестр рисков (7 колонок с длинными ячейками) уходит
    в landscape — это основной use case фичи.

    Маркер landscape — наличие reportlab-тега ``<pdf:nextpage name="landscape_page" />``
    в HTML (см. _wrap_in_landscape: CSS-класс @page-switching в xhtml2pdf
    игнорируется, поэтому используется именно этот тег).
    """
    md = (
        "| ID | Описание риска | Вероятность | Влияние | Митигация | Владелец | Срок |\n"
        "|---|---|---|---|---|---|---|\n"
        "| R-001 | Поставщик данных задерживает интеграцию из-за реорганизации | Средняя | Высокое | "
        "Закрепить SLA в контракте; держать буфер 2 недели в плане | Архитектор интеграций | 2026-06-01 |\n"
        "| R-002 | Регуляторное требование по локализации меняется в течение проекта | Низкая | "
        "Критическое | Юридическая проверка раз в квартал; гибкая архитектура | DPO | непрерывно |\n"
    )
    html = _md_to_html(md)
    out, landscape = _enhance_tables_in_html(html)

    assert landscape is True
    assert '<pdf:nextpage name="landscape_page"' in out
    assert "<colgroup>" in out


# ---------------------------------------------------------------------------
# Пропорциональность колонок
# ---------------------------------------------------------------------------


def test_long_text_column_gets_larger_share() -> None:
    """Колонка с длинным контентом получает большую долю ширины."""
    md = (
        "| Короткая | Очень длинная колонка с подробным описанием каждой строки |\n"
        "|---|---|\n"
        "| A | Здесь идёт развёрнутое предложение со множеством слов и подробностей |\n"
        "| B | И ещё одно длинное предложение, чтобы среднее было высоким однозначно |\n"
    )
    html = _md_to_html(md)
    out, _ = _enhance_tables_in_html(html)
    widths = _extract_widths(out)
    assert len(widths) == 2
    # Длинная колонка должна занимать существенно больше — минимум в 2 раза.
    assert widths[1] > widths[0] * 2


def test_narrow_column_has_minimum_floor() -> None:
    """Колонка с одним символом не должна сжиматься до невидимости."""
    md = (
        "| # | Очень-очень длинное описание с большим количеством информации |\n"
        "|---|---|\n"
        "| 1 | Развёрнутое содержание первой строки таблицы с подробностями |\n"
        "| 2 | Развёрнутое содержание второй строки таблицы с подробностями |\n"
    )
    html = _md_to_html(md)
    out, _ = _enhance_tables_in_html(html)
    widths = _extract_widths(out)
    assert len(widths) == 2
    assert widths[0] >= 5.0  # min-floor 5%


# ---------------------------------------------------------------------------
# Безопасность относительно битого / пустого HTML
# ---------------------------------------------------------------------------


def test_no_tables_no_extra_sections_adds_nothing() -> None:
    # Документ без таблиц и с одним разделом: оглавление не добавляется
    # (нужно >=2 раздела), таблиц нет — структурно ничего не прибавляется.
    # Точная байт-идентичность не проверяется: функция всегда re-сериализует
    # дерево (это нормально, xhtml2pdf принимает эквивалентный HTML).
    html = _md_to_html("# Просто текст\n\nБез таблиц.\n")
    out, landscape = _enhance_tables_in_html(html)
    assert landscape is False
    assert "Просто текст" in out
    assert "Без таблиц." in out
    assert 'class="doc-toc"' not in out
    assert "<colgroup>" not in out


def test_invalid_html_falls_back_to_original() -> None:
    """Если по какой-то причине HTML не парсится — экспорт не должен падать."""
    bad = "<table><tr><td>broken"
    out, landscape = _enhance_tables_in_html(bad)
    # Возвращён оригинал без модификации.
    assert out == bad
    assert landscape is False


def test_empty_table_skipped() -> None:
    """Таблица без строк не должна порождать пустой colgroup."""
    html = "<p>before</p><table></table><p>after</p>"
    out, landscape = _enhance_tables_in_html(html)
    assert landscape is False
    # colgroup НЕ вставлен в пустую таблицу.
    assert "<colgroup>" not in out


# ---------------------------------------------------------------------------
# Несколько таблиц: каждая обрабатывается независимо
# ---------------------------------------------------------------------------


def test_multiple_tables_processed_independently() -> None:
    """Inline-режим (extract_wide_tables=False): узкая таблица остаётся
    portrait, широкая в том же документе разворачивается в landscape на
    месте — оба факта независимы. (Вынос широких таблиц в приложение —
    дефолтный режим — покрыт в test_pdf_export.py.)"""
    narrow_md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    wide_md = (
        "| Длинная колонка X | Длинная колонка Y | Длинная колонка Z | "
        "Длинная колонка W | Длинная колонка V | Длинная колонка U | Длинная колонка T |\n"
        "|---|---|---|---|---|---|---|\n"
        "| Подробное содержание ячейки один два три | Аналогично два | "
        "Аналогично три | Аналогично четыре | Аналогично пять | "
        "Аналогично шесть | Аналогично семь |\n"
    )
    html = _md_to_html(narrow_md + "\n\n" + wide_md)
    out, landscape = _enhance_tables_in_html(html, extract_wide_tables=False)

    assert landscape is True
    # Должно быть ровно ОДНО открытие landscape-страницы — узкую таблицу
    # не трогаем. После широкой таблицы за ней нет контента → возврата
    # на body_page не нужно.
    assert out.count('<pdf:nextpage name="landscape_page"') == 1
    assert out.count('<pdf:nextpage name="body_page"') == 0
    # И два colgroup — по одному на каждую таблицу.
    assert out.count("<colgroup>") == 2


def test_landscape_wrap_inserts_return_to_body_when_content_follows() -> None:
    """Inline-режим: после широкой таблицы, если ниже есть ещё контент, в
    HTML появляется ``<pdf:nextpage name="body_page">`` для возврата к
    portrait."""
    md = (
        "| A | B | C | D | E | F | G |\n"
        "|---|---|---|---|---|---|---|\n"
        "| Длинная ячейка для расширения | Аналогично | Аналогично | "
        "Аналогично | Аналогично | Аналогично | Аналогично |\n\n"
        "## Раздел после таблицы\n\n"
        "Параграф, который должен оказаться на портретной странице.\n"
    )
    html = _md_to_html(md)
    out, landscape = _enhance_tables_in_html(html, extract_wide_tables=False)
    assert landscape is True
    assert '<pdf:nextpage name="landscape_page"' in out
    assert '<pdf:nextpage name="body_page"' in out


def test_landscape_wrap_skips_return_when_table_is_last() -> None:
    """Inline-режим: если за таблицей больше ничего нет — лишний пустой
    portrait page не нужен, возврат на body_page не вставляется."""
    md = (
        "| A | B | C | D | E | F | G |\n"
        "|---|---|---|---|---|---|---|\n"
        "| Длинная ячейка для расширения | Аналогично | Аналогично | "
        "Аналогично | Аналогично | Аналогично | Аналогично |\n"
    )
    html = _md_to_html(md)
    out, landscape = _enhance_tables_in_html(html, extract_wide_tables=False)
    assert landscape is True
    assert '<pdf:nextpage name="landscape_page"' in out
    assert '<pdf:nextpage name="body_page"' not in out


# ---------------------------------------------------------------------------
# Метрики (low-level)
# ---------------------------------------------------------------------------


def test_compute_column_metrics_handles_basic_table() -> None:
    """`_compute_column_metrics` должен корректно посчитать longest_word
    и mean_text_len по реальной структуре <table>."""
    html = _md_to_html("| короткая | длинная-колонка |\n|---|---|\n| a | bbbbb cccccc |\n")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(f"<root>{html}</root>")
    [table] = list(root.iter("table"))
    metrics = _compute_column_metrics(table)
    assert len(metrics) == 2
    # Первая колонка: только короткие значения.
    assert metrics[0].longest_word == len("короткая")
    # Вторая колонка: longest_word = len("длинная-колонка") = 15.
    assert metrics[1].longest_word == len("длинная-колонка")
    # mean_text_len второй колонки больше первой.
    assert metrics[1].mean_text_len > metrics[0].mean_text_len


def test_estimate_table_width_pt_grows_with_columns() -> None:
    """Чем больше колонок и/или больше weight в каждой — тем больше
    оценочная ширина в pt."""
    one_narrow = [_ColumnMetrics(longest_word=5, mean_text_len=5.0)]
    five_wide = [_ColumnMetrics(longest_word=40, mean_text_len=40.0) for _ in range(5)]
    assert _estimate_table_width_pt(five_wide) > _estimate_table_width_pt(one_narrow)
    # Базовая адекватность порогов: одна узкая колонка влезает в portrait.
    assert _estimate_table_width_pt(one_narrow) < _PORTRAIT_CONTENT_WIDTH_PT
    # А пять широких — не влезают даже в landscape (то есть точно > portrait).
    assert _estimate_table_width_pt(five_wide) > _PORTRAIT_CONTENT_WIDTH_PT


def test_landscape_content_width_constants_match_a4() -> None:
    """Регрессия на случай, если кто-то переставит margin'ы и забудет
    обновить пороги.

    A4 — 21×29.7 cm; 1 cm ≈ 28.35 pt.
      portrait: при margin 1.8 cm доступно ≈ 493 pt.
      landscape: при margin 1.0 cm (v3.8.3 — сжаты с боков под таблицы)
      доступно ≈ 785 pt."""
    assert 470 < _PORTRAIT_CONTENT_WIDTH_PT < 520
    # Landscape поля v3.8.3 узкие (1.0 cm), даём диапазон 700–810.
    assert 700 < _LANDSCAPE_CONTENT_WIDTH_PT < 810
