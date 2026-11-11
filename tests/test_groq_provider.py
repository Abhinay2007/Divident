import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
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
    assert calls.calls[0]["max_completion_tokens"] == 700
    assert calls.calls[0]["response_format"]["json_schema"]["strict"] is True
    assert provider.request_count == 1


def test_malformed_response_is_rejected_without_retrying_permanent_output():
    sdk, calls = client(["not json", "still not json", "also not json"])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(LLMSchemaViolation, match="malformed"):
        provider.structured_call("facts", {})
    assert len(calls.calls) == 1
    assert provider.request_count == 1


def test_rate_limit_retries_once_then_returns_sanitized_error():
    rate_limit = type("RateLimitError", (Exception,), {})
    sdk, calls = client([rate_limit("secret response"), rate_limit("secret response"), rate_limit("secret response")])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(RateLimitExceeded) as error:
        provider.structured_call("facts", {})
    assert error.value.code == "llm_rate_limited"
    assert "secret" not in str(error.value)
    assert len(calls.calls) == 2
    assert provider.request_count == 2


def test_timeout_retries_are_bounded():
    timeout = type("APITimeoutError", (Exception,), {})
    sdk, calls = client([timeout(), timeout(), timeout()])
    provider = GroqProvider("secret", "test-model", sdk, sleep=lambda _: None)
    with pytest.raises(LLMProviderError):
        provider.structured_call("facts", {})
    assert len(calls.calls) == 2
    assert provider.request_count == 2


def test_retry_without_provider_timing_uses_bounded_jitter():
    timeout = type("APITimeoutError", (Exception,), {})
    sdk, _ = client([timeout(), json.dumps({"ok": True})])
    pauses = []
    provider = GroqProvider("secret", "model", sdk, sleep=pauses.append,
                             jitter=lambda low, high: high)
    assert provider.structured_call("facts", {"type": "object"}) == {"ok": True}
    assert pauses == [0.5]


def test_input_over_limit_is_rejected_before_network_call():
    sdk, calls = client([json.dumps({"ok": True})])
    provider = GroqProvider("secret", "model", sdk)
    with pytest.raises(LLMSchemaViolation) as error:
        provider.structured_call("x" * 4000, {"type": "object"})
    assert error.value.code == "llm_input_budget_exceeded"
    assert calls.calls == []


def test_only_one_groq_request_runs_concurrently():
    lock = Lock()
    active = maximum = 0
    class Completions:
        def create(self, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"ok":true}'))])
    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = GroqProvider("secret", "model", client)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: provider.structured_call("facts", {"type": "object"}), range(3)))
    assert results == [{"ok": True}] * 3
    assert maximum == 1
