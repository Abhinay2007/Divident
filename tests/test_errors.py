import pytest

from dividend.errors import (
    CaptureMismatch, ConfigurationError, DividendError, EnrichmentError,
    InvariantViolation, LLMDisabled, LLMSchemaViolation, NotFoundError,
    PaymentProviderError, PayoutRejected, ProviderTimeout, RateLimitExceeded,
    StateError, ValidationError,
)


def test_exception_inheritance_and_common_attributes():
    classes = [ValidationError, StateError, NotFoundError, ConfigurationError,
               PaymentProviderError, LLMSchemaViolation, EnrichmentError,
               RateLimitExceeded, LLMDisabled, InvariantViolation]
    assert all(issubclass(cls, DividendError) for cls in classes)
    assert issubclass(CaptureMismatch, PaymentProviderError)
    assert issubclass(PayoutRejected, PaymentProviderError)
    assert issubclass(ProviderTimeout, PaymentProviderError)
    error = ValidationError("bad input", code="INPUT_INVALID", detail={"field": "mode"})
    assert (error.code, error.detail, str(error)) == ("INPUT_INVALID", {"field": "mode"}, "bad input")


def test_state_error_uses_domain_allowed_transitions_name():
    error = StateError("cannot fund", code="STATE_INVALID", current_state="draft", allowed_transitions=["approve"])
    assert error.current_state == "draft"
    assert error.allowed_transitions == ["approve"]
    with pytest.raises(TypeError):
        StateError(code="STATE_INVALID", current_state="draft", allowed_actions=["approve"])


def test_payment_retry_hint_implementation_contract():
    error = ProviderTimeout("temporary provider timeout", code="PROVIDER_TIMEOUT", retry_hint="retry_same_attempt")
    assert error.retry_hint == "retry_same_attempt"
    assert error.detail == {}
    with pytest.raises(TypeError):
        PaymentProviderError(code="PROVIDER", retry_after_seconds=3)
