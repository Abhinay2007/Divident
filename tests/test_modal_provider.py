import json
import logging
from concurrent.futures import ThreadPoolExecutor
import sys
from threading import Lock
import time
from types import SimpleNamespace

import pytest

from dividend.errors import LLMProviderError, LLMSchemaViolation, RateLimitExceeded
from dividend.llm.modal_provider import ModalProvider, parse_structured_output


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


def response_client(response):
    completions = SimpleNamespace(create=lambda **kwargs: response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_client_uses_modal_endpoint_and_proxy_token_pair(monkeypatch):
    captured = {}
    sdk, _ = client([])

    def openai_factory(**kwargs):
        captured.update({
            "base_url_ok": kwargs.get("base_url") == "https://modal.test/v1",
            "token_pair_ok": kwargs.get("api_key") == "proxy-id.proxy-secret",
            "retries_disabled": kwargs.get("max_retries") == 0,
        })
        return sdk

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_factory))
    monkeypatch.setenv("MODAL_ENDPOINT_URL", "https://modal.test/v1")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "proxy-id")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "proxy-secret")
    ModalProvider("model")
    assert captured == {"base_url_ok": True, "token_pair_ok": True, "retries_disabled": True}


def test_sends_strict_schema_reasoning_none_and_parses_json():
    sdk, calls = client([json.dumps({"ok": True})])
    provider = ModalProvider("deepseek-ai/DeepSeek-V4.1-Flash-1017", sdk)
    result = provider.structured_call("facts", {"type": "object"}, timeout=7)
    assert result == {"ok": True}
    assert calls.calls[0]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    assert calls.calls[0]["timeout"] == 7
    assert calls.calls[0]["max_tokens"] == 4096
    assert calls.calls[0]["temperature"] == 0.3
    assert calls.calls[0]["top_p"] == 0.9
    assert calls.calls[0]["stream"] is False
    assert calls.calls[0]["response_format"]["json_schema"]["strict"] is True
    assert calls.calls[0]["extra_body"] == {"reasoning_effort": "low"}
    assert provider.request_count == 1


def test_normal_assistant_text_logs_only_safe_response_metadata(caplog):
    secret_response_text = '{"ok":true}'
    response = SimpleNamespace(
        model="deepseek-ai/DeepSeek-V4.1-Flash-1017",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=secret_response_text, reasoning_content="private reasoning", tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2)),
    )
    caplog.set_level(logging.INFO)
    result = ModalProvider("model", response_client(response)).structured_call("facts", {})
    assert result == {"ok": True}
    assert "request_status=success" in caplog.text
    assert "returned_model=deepseek-ai/DeepSeek-V4.1-Flash-1017" in caplog.text
    assert "choices_count=1" in caplog.text
    assert "finish_reason=stop" in caplog.text
    assert "content_present=True" in caplog.text
    assert "content_type=str" in caplog.text
    assert "content_chars=11" in caplog.text
    assert "reasoning_content_exists=True" in caplog.text
    assert "reasoning_content_chars=17" in caplog.text
    assert "tool_calls_exists=False" in caplog.text
    assert "tool_call_count=0" in caplog.text
    assert "usage_total_tokens=13" in caplog.text
    assert "usage_reasoning_tokens=2" in caplog.text
    assert secret_response_text not in caplog.text
    assert "private reasoning" not in caplog.text


@pytest.mark.parametrize(("content", "reason"), [
    (None, "non_text_content"),
    ("", "empty_text"),
    (" \n ", "empty_text"),
    ([], "non_text_content"),
])
def test_invalid_content_type_or_empty_text_is_rejected_without_parsed_fallback(content, reason):
    message = SimpleNamespace(content=content, parsed={"ok": True})
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client(response)).structured_call("facts", {})
    assert error.value.detail == {"field": "content", "reason": reason}


def test_missing_choices_is_reported():
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client(SimpleNamespace())).structured_call("facts", {})
    assert error.value.detail == {"field": "choices", "reason": "missing_or_invalid_choices"}


def test_empty_choices_is_reported():
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client(SimpleNamespace(choices=[]))).structured_call("facts", {})
    assert error.value.detail == {"field": "choices", "reason": "empty_choices"}


def test_unexpected_response_structure_is_reported_safely():
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client({"choices": []})).structured_call("facts", {})
    assert error.value.detail == {"field": "choices", "reason": "missing_or_invalid_choices"}


def test_reasoning_only_response_is_not_used_as_answer(caplog):
    secret_reasoning = '{"ok":true}'
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=
        SimpleNamespace(content=None, reasoning_content=secret_reasoning))])
    caplog.set_level(logging.INFO)
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client(response)).structured_call("facts", {})
    assert error.value.detail == {"field": "content", "reason": "non_text_content"}
    assert "reasoning_content_exists=True" in caplog.text
    assert "reasoning_content_chars=11" in caplog.text
    assert secret_reasoning not in caplog.text


def test_truncated_completion_is_rejected_even_when_content_looks_like_json():
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=
        SimpleNamespace(content='{"ok":'))])
    with pytest.raises(LLMSchemaViolation) as error:
        ModalProvider("model", response_client(response)).structured_call("facts", {})
    assert error.value.detail == {"field": "content", "reason": "completion_truncated"}


@pytest.mark.parametrize("response", [
    '{"ok":true}',
    '```json\n{"ok":true}\n```',
    'Here is the requested result:\n{"ok":true}\nEnd of response.',
])
def test_deepseek_json_formats_are_parsed_without_loosening_object_shape(response):
    assert parse_structured_output(response) == {"ok": True}


def test_malformed_json_reports_safe_validation_detail():
    with pytest.raises(LLMSchemaViolation) as error:
        parse_structured_output('```json\n{"ok":}\n```')
    assert error.value.detail == {"field": "content", "reason": "malformed_json"}


def test_surrounding_unsupported_json_text_is_rejected():
    with pytest.raises(LLMSchemaViolation) as error:
        parse_structured_output('Result {"ok":true} extra {"other":false}')
    assert error.value.detail["reason"] == "ambiguous_text_outside_json"


def test_malformed_response_is_rejected_without_retrying():
    sdk, calls = client(["not json", "still not json"])
    provider = ModalProvider("model", sdk)
    with pytest.raises(LLMSchemaViolation, match="malformed"):
        provider.structured_call("facts", {})
    assert len(calls.calls) == provider.request_count == 1


@pytest.mark.parametrize(("error_type", "status", "code"), [
    ("RateLimitError", 429, "modal_rate_limited"),
    ("AuthenticationError", 401, "modal_authentication_failed"),
    ("APITimeoutError", None, "modal_timeout"),
    ("APIConnectionError", None, "modal_endpoint_unavailable"),
    ("InternalServerError", 503, "modal_endpoint_unavailable"),
    ("BadRequestError", 400, "modal_request_failed"),
])
def test_provider_failures_are_sanitized_and_not_retried(error_type, status, code):
    error_class = type(error_type, (Exception,), {"status_code": status})
    sdk, calls = client([error_class("private response")])
    provider = ModalProvider("model", sdk)
    with pytest.raises((RateLimitExceeded, LLMProviderError)) as exc:
        provider.structured_call("facts", {})
    assert exc.value.code == code
    assert "private response" not in str(exc.value)
    assert len(calls.calls) == provider.request_count == 1


def test_unsupported_request_parameter_is_sanitized_and_not_retried():
    error_class = type("BadRequestError", (Exception,), {"status_code": 400})
    sdk, calls = client([error_class("Unsupported parameter: top_p; private payload")])
    provider = ModalProvider("deepseek-ai/DeepSeek-V4.1-Flash-1017", sdk)
    with pytest.raises(LLMProviderError) as error:
        provider.structured_call("facts", {})
    assert error.value.code == "modal_request_failed"
    assert "private payload" not in str(error.value)
    assert len(calls.calls) == provider.request_count == 1


def test_input_over_limit_is_rejected_before_network_call():
    sdk, calls = client([json.dumps({"ok": True})])
    provider = ModalProvider("model", sdk)
    with pytest.raises(LLMSchemaViolation) as error:
        provider.structured_call("context for review " * 2500, {"type": "object"})
    assert error.value.code == "llm_input_budget_exceeded"
    assert calls.calls == []


def test_only_one_modal_request_runs_concurrently():
    lock = Lock()
    active = maximum = 0

    class ConcurrentCompletions:
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

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=ConcurrentCompletions()))
    provider = ModalProvider("model", sdk)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: provider.structured_call("facts", {}), range(3)))
    assert results == [{"ok": True}] * 3
    assert maximum == 1
