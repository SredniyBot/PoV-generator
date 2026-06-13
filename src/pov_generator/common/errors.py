class PovGeneratorError(Exception):
    """Base application error."""


class ValidationError(PovGeneratorError):
    """Raised when declarative inputs are invalid."""


class NotFoundError(PovGeneratorError):
    """Raised when a requested object cannot be resolved."""


class ConflictError(PovGeneratorError):
    """Raised when a state transition or patch is inconsistent."""


class ProviderExhaustedError(PovGeneratorError):
    """LLM-провайдер исчерпал квоту/лимит (rate-limit окна подписки, 429/529).

    Намеренно НЕ наследник :class:`ConflictError`: retry-петли (которые ловят
    ConflictError) её НЕ перехватывают — повтор бессмыслен, лимит не вернётся за
    секунды. Раннер ловит её как ФАТАЛЬНУЮ для прогона: дальнейшие задачи не
    запускаются (нет смысла — подписка исчерпана), пользователю показывается
    явное сообщение. Поднимать только когда у задачи нет рабочего альтернативного
    провайдера.
    """
