"""Тесты визуального превью стиля в UI/UX-документе.

Из дизайн-системы (палитра/типографика) генерируется SVG-мокап окна и базовых
элементов, встраивается как data-URI <img> — рендерится и на сайте (marked
пропускает raw HTML), и в PDF (xhtml2pdf+svglib, как mermaid-картинки)."""
from __future__ import annotations

import base64
import re

from pov_generator.application.artifact_contracts import (
    _build_ui_preview_svg,
    render_markdown,
)

_PAYLOAD = {
    "summary": "Дизайн интерфейса.",
    "color_palette": [
        {"name": "Бренд", "hex": "#3b6ef5", "role": "Акцент"},
        {"name": "Фон", "hex": "#f4f6f9", "role": "Фон"},
        {"name": "Текст", "hex": "#1f2430", "role": "Текст"},
    ],
    "typography": [{"role": "Заголовок", "font": "Inter"}],
    "screens": [{"name": "Главный", "purpose": "обзор"}],
}


def test_preview_svg_uses_real_palette_and_is_well_formed() -> None:
    svg = _build_ui_preview_svg(_PAYLOAD)
    assert svg is not None
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "#3b6ef5" in svg  # акцент использован как цвет кнопки
    # Только svglib-безопасные примитивы (без градиентов/фильтров/foreignObject).
    for forbidden in ("foreignObject", "<linearGradient", "<filter", "<style"):
        assert forbidden not in svg


def test_ui_design_doc_embeds_preview_for_both_surfaces() -> None:
    md = render_markdown("ui_design", _PAYLOAD)
    assert "## Превью стиля" in md
    assert 'class="ui-preview"' in md
    assert "data:image/svg+xml;base64," in md
    # data-URI декодируется в валидный SVG.
    match = re.search(r"base64,([A-Za-z0-9+/=]+)", md)
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    assert decoded.startswith("<svg") and "</svg>" in decoded


def test_no_palette_no_preview() -> None:
    md = render_markdown("ui_design", {"summary": "x", "screens": [{"name": "a", "purpose": "b"}]})
    assert "Превью стиля" not in md
    assert _build_ui_preview_svg({"screens": []}) is None
