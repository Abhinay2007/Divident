import json
from types import SimpleNamespace

import pytest

from dividend.errors import LLMProviderError, LLMSchemaViolation, RateLimitExceeded
from dividend.llm.groq_provider import GroqProvider


class Completions:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value))])


def client(values):
    completions = Completions(values)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_sends_strict_schema_and_parses_json():
    response = json.dumps({"proposals": []})
    sdk, calls = client([response])
    provider = GroqProvider("secret", "test-model", sdk)
    result = provider.structured_call("facts", {"type": "object"}, timeout=7)
    assert result == {"proposals": []}
    assert calls.calls[0]["model"] == "test-model"
    assert calls.calls[0]["timeout"] == 7
    assert calls.calls[0]["response_format"]["json_schema"]["strict"] is True
    assert provider.request_count == 1


def test_malformed_response_is_rejected_without_leaking_provider_details():
    sdk, calls = client(["not json", "still not json", "also not json"])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(LLMSchemaViolation, match="malformed"):
        provider.structured_call("facts", {})
    assert len(calls.calls) == 3
    assert provider.request_count == 3


def test_rate_limit_retries_three_times_then_returns_sanitized_error():
    rate_limit = type("RateLimitError", (Exception,), {})
    sdk, calls = client([rate_limit("secret response"), rate_limit("secret response"), rate_limit("secret response")])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(RateLimitExceeded) as error:
        provider.structured_call("facts", {})
    assert error.value.code == "llm_rate_limited"
    assert "secret" not in str(error.value)
    assert len(calls.calls) == 3
    assert provider.request_count == 3


def test_timeout_retries_are_bounded():
    timeout = type("APITimeoutError", (Exception,), {})
    sdk, calls = client([timeout(), timeout(), timeout()])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(LLMProviderError):
        provider.structured_call("facts", {})
    assert len(calls.calls) == 3
    assert provider.request_count == 3
