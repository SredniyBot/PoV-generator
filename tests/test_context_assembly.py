"""Юнит-тесты укладчика контекста (context_assembly).

Проверяем инвариант: закреплённое (источник истины / обязательное) не
выкидывается; производное — по авторитету до бюджета; выкинутое фиксируется;
финальный порядок — по display_order; бюджет выводится из окна модели.
"""

from __future__ import annotations

from pov_generator.application.context_assembly import (
    OUTPUT_RESERVE_TOKENS,
    ContextAuthority,
    ContextCandidate,
    effective_input_budget,
    pack_context,
)
from pov_generator.domain.artifacts import ContextItem


def _item(title: str, tokens: int, priority: int = 0) -> ContextItem:
    return ContextItem(
        item_id=title,
        item_type="problem_field",
        source_ref=title,
        title=title,
        content="x" * (tokens * 4),
        token_estimate=tokens,
        required=False,
        priority=priority,
    )


def _cand(title, tokens, authority, pinned, order) -> ContextCandidate:
    return ContextCandidate(_item(title, tokens), authority, pinned=pinned, display_order=order)


def test_pinned_always_in_droppable_by_authority() -> None:
    cands = [
        _cand("source", 100, ContextAuthority.CUSTOMER_INPUT, True, 0),
        _cand("vendor", 100, ContextAuthority.REFERENCE, False, 1),
        _cand("fact", 100, ContextAuthority.DERIVED, False, 2),
    ]
    result = pack_context(cands, budget_tokens=250)  # хватает на pinned + 1 droppable
    titles = [it.title for it in result.items]
    assert "source" in titles            # закреплённое — всегда
    assert "fact" in titles              # выше по авторитету
    assert "vendor" not in titles        # ниже по авторитету → выкинут
    assert any("vendor" in e for e in result.excluded)
    assert result.over_budget is False


def test_required_over_budget_is_flagged_not_dropped() -> None:
    # Обязательное (не источник) не усекаем — если не влезает, это настоящий отказ.
    cands = [_cand("must", 500, ContextAuthority.REQUIRED, True, 0)]
    result = pack_context(cands, budget_tokens=100)
    assert "must" in [it.title for it in result.items]  # не выкидываем
    assert result.over_budget is True                   # помечаем переполнение


def test_oversized_source_is_trimmed_not_failed() -> None:
    # Источник истины при нехватке места УСЕКАЕТСЯ (видимо), а не роняет укладку.
    big = _item("source", 500)
    cands = [ContextCandidate(big, ContextAuthority.CUSTOMER_INPUT, pinned=True, display_order=0)]
    result = pack_context(cands, budget_tokens=100)
    item = next(it for it in result.items if it.title == "source")
    assert len(item.content) < len(big.content)          # усечён
    assert "усечён" in item.content                      # с явной пометкой
    assert any("source" in e and "усечён" in e for e in result.excluded)  # видимо в аудите
    assert result.over_budget is False                   # не отказ — уложились


def test_final_order_by_display_order_not_authority() -> None:
    cands = [
        _cand("b", 10, ContextAuthority.DERIVED, False, 1),
        _cand("a", 10, ContextAuthority.REFERENCE, False, 0),
    ]
    result = pack_context(cands, budget_tokens=1000)
    assert [it.title for it in result.items] == ["a", "b"]  # по display_order


def test_no_budget_includes_everything() -> None:
    cands = [_cand("x", 9999, ContextAuthority.REFERENCE, False, 0)]
    result = pack_context(cands, budget_tokens=None)
    assert [it.title for it in result.items] == ["x"]
    assert result.excluded == ()


def test_effective_input_budget() -> None:
    # Большое окно: главное — намерение шаблона.
    assert effective_input_budget(12000, 200_000) == 12000
    # Без шаблона: окно минус резерв на вывод.
    assert effective_input_budget(None, 200_000) == 200_000 - OUTPUT_RESERVE_TOKENS
    # Маленькое окно ограничивает намерение шаблона.
    assert effective_input_budget(64000, 40_000) == 40_000 - OUTPUT_RESERVE_TOKENS
    # Ничего не задано — без лимита.
    assert effective_input_budget(None, None) is None
    # Жёсткий потолок (провайдер с лимитом окна) срезает раздутый контекст,
    # даже когда окно/шаблон щедрее.
    assert effective_input_budget(None, 200_000, 48_000) == 48_000
    assert effective_input_budget(120_000, 200_000, 48_000) == 48_000
    # Но если шаблон/окно строже жёсткого потолка — берём строжайшее.
    assert effective_input_budget(12_000, 200_000, 48_000) == 12_000
    # Жёсткий потолок без прочих лимитов.
    assert effective_input_budget(None, None, 48_000) == 48_000
