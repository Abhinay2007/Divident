"""Modal Dedicated Endpoint adapter for strict structured responses."""

import json
import logging
import os
import re
from threading import BoundedSemaphore

from ..errors import LLMProviderError, LLMSchemaViolation, RateLimitExceeded

_REQUEST_GATE = BoundedSemaphore(1)
_logger = logging.getLogger(__name__)
INPUT_TOKEN_LIMIT = 3000
MAX_TOKENS = 4096
REASONING_EFFORT = "low"
_JSON_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
_MISSING = object()
_FINISH_REASONS = {"stop", "length", "tool_calls", "function_call", "content_filter"}


def count_input_tokens(prompt: str, schema: dict) -> int:
    """Count prompt and schema with tokenizer plus framing headroom."""
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(encoding.encode(prompt)) + len(encoding.encode(schema_text)) + 128


def _response_diagnostics(response) -> dict:
    choices = getattr(response, "choices", _MISSING)
    choices_count = len(choices) if isinstance(choices, (list, tuple)) else None
    choice = choices[0] if choices_count else None
    message = getattr(choice, "message", None) if choice is not None else None
    content = getattr(message, "content", _MISSING) if message is not None else _MISSING
    reasoning = getattr(message, "reasoning_content", _MISSING) if message is not None else _MISSING
    tool_calls = getattr(message, "tool_calls", _MISSING) if message is not None else _MISSING
    usage = getattr(response, "usage", None)
    finish_reason = getattr(choice, "finish_reason", None) if choice is not None else None
    returned_model = getattr(response, "model", None)
    return {
        "returned_model": (
            returned_model if type(returned_model) is str
            and re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", returned_model) else "unknown"
        ),
        "choices_count": choices_count,
        "finish_reason": (
            finish_reason if type(finish_reason) is str and finish_reason in _FINISH_REASONS
            else "other" if finish_reason is not None else "missing"
        ),
        "content_present": content is not _MISSING and content is not None,
        "content_type": "missing" if content is _MISSING else type(content).__name__,
        "content_chars": len(content) if type(content) is str else None,
        "reasoning_content_exists": reasoning is not _MISSING and reasoning is not None,
        "reasoning_content_chars": len(reasoning) if type(reasoning) is str else None,
        "tool_calls_exists": (
            bool(tool_calls) if isinstance(tool_calls, (list, tuple))
            else tool_calls is not _MISSING and tool_calls is not None
        ),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, (list, tuple)) else 0,
        "usage_prompt_tokens": _usage_count(usage, "prompt_tokens"),
        "usage_completion_tokens": _usage_count(usage, "completion_tokens"),
        "usage_reasoning_tokens": _reasoning_token_count(usage),
        "usage_total_tokens": _usage_count(usage, "total_tokens"),
    }


def _usage_count(usage, field: str) -> int | None:
    value = getattr(usage, field, None) if usage is not None else None
    return value if type(value) is int else None


def _reasoning_token_count(usage) -> int | None:
    details = getattr(usage, "completion_tokens_details", None) if usage is not None else None
    value = getattr(details, "reasoning_tokens", None) if details is not None else None
    return value if type(value) is int else None


def parse_structured_output(content: str) -> dict:
    def reject(reason: str):
        raise LLMSchemaViolation(
            "Modal returned a malformed structured response",
            code="llm_schema_violation",
            detail={"field": "content", "reason": reason},
        )

    if type(content) is not str:
        reject("non_text_content")
    if not content.strip():
        reject("empty_text")
    text = content.strip()
    fences = list(_JSON_FENCE.finditer(text))
    if "```" in text:
        if (len(fences) != 1 or text.count("```") != 2
                or fences[0].group(1).strip().lower() not in {"", "json"}):
            reject("unsupported_or_ambiguous_code_fence")
        fence = fences[0]
        before, after = text[:fence.start()].strip(), text[fence.end():].strip()
        if any(char in before + after for char in "{}[]"):
            reject("ambiguous_text_outside_json")
        text = fence.group(2).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            reject("malformed_json")
    else:
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start < 0:
            reject("missing_json_object")
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            reject("malformed_json")
        if any(char in text[:start] + text[start + end:] for char in "{}[]"):
            reject("ambiguous_text_outside_json")
    if type(parsed) is not dict:
        reject("expected_object")
    return parsed


class ModalProvider:
    def __init__(self, model: str, client=None, budget=None, *,
                 endpoint_url: str | None = None,
                 proxy_token_id: str | None = None,
                 proxy_token_secret: str | None = None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                base_url=endpoint_url or os.environ["MODAL_ENDPOINT_URL"],
                api_key=(
                    f"{proxy_token_id or os.environ['MODAL_PROXY_TOKEN_ID']}."
                    f"{proxy_token_secret or os.environ['MODAL_PROXY_TOKEN_SECRET']}"
                ),
                max_retries=0,
            )
        self.client = client
        self.model = model
        self.budget = budget
        self.request_count = 0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def structured_call(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        with _REQUEST_GATE:
            input_tokens = count_input_tokens(prompt, schema)
            if input_tokens > INPUT_TOKEN_LIMIT:
                raise LLMSchemaViolation(
                    "Modal input exceeds the 3000-token safety limit",
                    code="llm_input_budget_exceeded",
                )
            if self.budget is not None:
                self.budget.reserve(input_tokens)
            self.request_count += 1
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    timeout=timeout,
                    max_tokens=MAX_TOKENS,
                    top_p=0.9,
                    stream=False,
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "structured_response", "schema": schema, "strict": True,
                    }},
                    extra_body={"reasoning_effort": REASONING_EFFORT},
                )
            except Exception as exc:
                name = type(exc).__name__.lower()
                status = getattr(exc, "status_code", None)
                _logger.warning(
                    "Modal request request_status=failure http_status=%s",
                    status if type(status) is int else "unknown",
                )
                if status == 429 or "ratelimit" in name or "rate_limit" in name:
                    raise RateLimitExceeded(
                        "Modal endpoint rate limit exceeded", code="modal_rate_limited",
                    ) from None
                if status in (401, 403) or "authentication" in name or "permissiondenied" in name:
                    raise LLMProviderError(
                        "Modal endpoint authentication failed", code="modal_authentication_failed",
                    ) from None
                if "timeout" in name:
                    raise LLMProviderError("Modal endpoint timed out", code="modal_timeout") from None
                if "connection" in name or status is not None and status >= 500:
                    raise LLMProviderError(
                        "Modal endpoint is unavailable", code="modal_endpoint_unavailable",
                    ) from None
                raise LLMProviderError(
                    "Modal endpoint rejected the request", code="modal_request_failed",
                ) from None
            diagnostics = _response_diagnostics(response)
            _logger.info("Modal response request_status=success %s", " ".join(
                f"{key}={value}" for key, value in diagnostics.items()
            ))
            if diagnostics["finish_reason"] == "length":
                raise LLMSchemaViolation(
                    "Modal completion reached its token limit before a complete response",
                    code="llm_schema_violation",
                    detail={"field": "content", "reason": "completion_truncated"},
                ) from None
            choices = getattr(response, "choices", _MISSING)
            if choices is _MISSING or not isinstance(choices, (list, tuple)):
                raise LLMSchemaViolation(
                    "Modal returned a malformed structured response",
                    code="llm_schema_violation",
                    detail={"field": "choices", "reason": "missing_or_invalid_choices"},
                ) from None
            if not choices:
                raise LLMSchemaViolation(
                    "Modal returned no completion choices",
                    code="llm_schema_violation",
                    detail={"field": "choices", "reason": "empty_choices"},
                ) from None
            message = getattr(choices[0], "message", _MISSING)
            if message is _MISSING or message is None:
                raise LLMSchemaViolation(
                    "Modal returned no assistant message",
                    code="llm_schema_violation",
                    detail={"field": "message", "reason": "missing_message"},
                ) from None
            content = getattr(message, "content", _MISSING)
            if content is _MISSING:
                raise LLMSchemaViolation(
                    "Modal response has no assistant content field",
                    code="llm_schema_violation",
                    detail={"field": "content", "reason": "missing_content"},
                ) from None
            return parse_structured_output(content)
