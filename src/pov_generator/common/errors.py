class PovGeneratorError(Exception):
    """Base application error."""


class ValidationError(PovGeneratorError):
    """Raised when declarative inputs are invalid."""


class NotFoundError(PovGeneratorError):
    """Raised when a requested object cannot be resolved."""


class ConflictError(PovGeneratorError):
    """Raised when a state transition or patch is inconsistent."""
