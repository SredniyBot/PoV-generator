"""Авторинг умений: простой способ расширять каталог.

Зона роста (пробел в умениях) — это кандидат на новое умение. Чтобы расширение
было дешёвым, система готовит **черновик профиля умения** из пробела: человеку
остаётся проверить и сохранить файл в ``templates/capabilities/``. Новое умение
заводится сразу как **пробное** (``maturity: experimental``) — используется с
осторожностью, а до «надёжного» дорастает по успешным сдачам (отдельный шаг).

Здесь — чистая, детерминированная генерация YAML (без LLM и без записи на диск):
функцию вызывают CLI/служба, которые решают, куда положить результат. Так
авторинг тестируем и не смешивает реестр с рантаймом.
"""

from __future__ import annotations

from collections.abc import Sequence

# Допустимые направления реализации (должны совпадать с _CAPABILITY_ROLES
# в domain/registry). Держим список рядом с авторингом для понятной ошибки.
_ROLES = ("backend", "ui", "ml", "data", "integration")


def _yaml_list(items: Sequence[str], indent: str) -> str:
    """Простой YAML-список строк (без внешних зависимостей сериализации)."""
    rows = [item.strip() for item in items if item and item.strip()]
    if not rows:
        return " []"
    return "\n" + "\n".join(f"{indent}- {row}" for row in rows)


def draft_trial_capability_profile(
    *,
    capability_id: str,
    title: str,
    role: str,
    capability_name: str,
    tech: Sequence[str] = (),
    requires: Sequence[str] = (),
    cannot_do: Sequence[str] = (),
) -> str:
    """Сформировать YAML-черновик профиля умения как **пробного**.

    Результат — валидный ``kind: capability_profile`` (проходит
    ``parse_capability_profile``), готовый к ревью и сохранению в реестр.

    Raises:
        ValueError: пустой обязательный аргумент или неизвестное направление.
    """
    capability_id = capability_id.strip()
    title = title.strip()
    capability_name = capability_name.strip()
    if not capability_id:
        raise ValueError("capability_id обязателен")
    if not title:
        raise ValueError("title обязателен")
    if not capability_name:
        raise ValueError("capability_name обязателен")
    if role not in _ROLES:
        raise ValueError(f"role должен быть одним из {list(_ROLES)}")

    # cannot_do не может быть пустым в контракте — даём осмысленный дефолт-плейсхолдер.
    limits_note = ["Уточнить пределы (надёжность/точность/объём) до повышения до надёжного"]
    cannot = list(cannot_do) or ["Уточнить ограничения умения до повышения до надёжного"]

    return (
        f"# Черновик пробного умения из зоны роста. Перед сохранением в реестр:\n"
        f"# добавьте '{capability_name}' в templates/vocabularies/capabilities.yaml\n"
        f"# и уточните tech / requires / limits / cannot_do.\n"
        f"kind: capability_profile\n"
        f"id: {capability_id}\n"
        f"version: 1.0.0\n"
        f"title: {title}\n"
        f"role: {role}\n"
        f"capabilities:\n"
        f"  - capability: {capability_name}\n"
        f"    maturity: experimental  # пробное: до надёжного — по успешным сдачам\n"
        f"    tech:{_yaml_list(tech, '      ')}\n"
        f"    requires:{_yaml_list(requires, '      ')}\n"
        f"    limits:\n"
        f"      note: {limits_note[0]}\n"
        f"cannot_do:{_yaml_list(cannot, '  ')}\n"
    )
