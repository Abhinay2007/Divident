"""Framework-independent domain exceptions."""


class DividendError(Exception):
    def __init__(self, message: str = "Dividend error", *, code: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class ValidationError(DividendError):
    pass


class StateError(DividendError):
    def __init__(self, message: str = "Invalid state", *, code: str, current_state: str, allowed_transitions: list[str] | None = None, detail: dict | None = None):
        self.current_state = current_state
        self.allowed_transitions = allowed_transitions or []
        super().__init__(message, code=code, detail=detail)


class NotFoundError(DividendError):
    pass


class ConfigurationError(DividendError):
    pass


class PaymentProviderError(DividendError):
    """Provider failure; retry_hint is a nullable string for later HTTP mapping."""
    def __init__(self, message: str = "Payment provider error", *, code: str, retry_hint: str | None = None, detail: dict | None = None):
        self.retry_hint = retry_hint
        super().__init__(message, code=code, detail=detail)


class CaptureMismatch(PaymentProviderError):
    pass


class PayoutRejected(PaymentProviderError):
    pass


class ProviderTimeout(PaymentProviderError):
    pass


class LLMSchemaViolation(DividendError):
    pass


class EnrichmentError(DividendError):
    pass


class RateLimitExceeded(DividendError):
    pass


class LLMDisabled(DividendError):
    pass


class InvariantViolation(DividendError):
    pass
