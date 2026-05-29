"""Только ParticipationMode — режим участия пользователя.

Все остальные сущности (ClarificationRequest, ClarificationOption,
ClarificationCandidate, DecisionOwnerRole и т.д.) удалены в v3.1 миграции
на Decision-модель (см. domain/decisions.py).
"""

from typing import Literal

ClarificationMode = Literal["autopilot", "balanced", "control", "expert"]
"""Режим участия пользователя — определяет, какие уровни Decision
показываются ему в checkpoint-сессиях.

Имя ClarificationMode сохранено для backward-compat с
process_state.py и т.п., хотя семантика в v3.1 шире — он определяет
не только clarifications, а вообще всю интерактивность."""
