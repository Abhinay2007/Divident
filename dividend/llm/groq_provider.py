"""Groq Chat Completions adapter for strict JSON Schema proposals."""

import json
import random
import time
from threading import BoundedSemaphore
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime

from ..errors import LLMProviderError, LLMSchemaViolation, RateLimitExceeded

_REQUEST_GATE = BoundedSemaphore(1)


class GroqProvider:
    def __init__(self, api_key: str, model: str, client=None, sleep=time.sleep,
                 jitter=random.uniform, budget=None):
        if client is None:
            from groq import Groq
            client = Groq(api_key=api_key, max_retries=0)
        self.client = client
        self.model = model
        self.sleep = sleep
        self.jitter = jitter
        self.budget = budget
        self.request_count = 0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def structured_call(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        with _REQUEST_GATE:
            return self._structured_call(prompt, schema, timeout)

    def _structured_call(self, prompt: str, schema: dict, timeout: int) -> dict:
        input_bytes = len(prompt.encode("utf-8")) + len(json.dumps(
            schema, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")) + 128
        if input_bytes > 3000:
            raise LLMSchemaViolation("Groq input exceeds the 3000-token safety limit",
                                     code="llm_input_budget_exceeded")
        for attempt in range(2):
            if self.budget is not None:
                self.budget.reserve(input_bytes)
            try:
                self.request_count += 1
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    timeout=timeout,
                    max_completion_tokens=700,
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "structured_response", "schema": schema, "strict": True,
                    }},
                )
                content = response.choices[0].message.content
                parsed = json.loads(content)
                if type(parsed) is not dict:
                    raise ValueError("unexpected JSON shape")
                return parsed
            except Exception as exc:
                name = type(exc).__name__.lower()
                status = getattr(exc, "status_code", None)
                rate_limited = status == 429 or "ratelimit" in name or "rate_limit" in name
                transient = (rate_limited or status is not None and status >= 500
                             or any(word in name for word in ("timeout", "connection")))
                retry_after = self._retry_after(exc) if rate_limited else None
                if transient and attempt == 0:
                    delay = min(2.0, 0.5 * (2 ** attempt))
                    self.sleep(retry_after if retry_after is not None
                               else self.jitter(0.0, delay))
                    continue
                if rate_limited:
                    raise RateLimitExceeded("Groq rate limit exceeded", code="llm_rate_limited",
                                            retry_after_seconds=retry_after) from None
                if transient:
                    raise LLMProviderError("Groq request failed", code="llm_provider_unavailable") from None
                if isinstance(exc, (json.JSONDecodeError, ValueError, IndexError,
                                    AttributeError, TypeError)):
                    raise LLMSchemaViolation("Groq returned a malformed structured response", code="llm_schema_violation") from None
                raise LLMProviderError("Groq request failed", code="llm_provider_error") from None

    @staticmethod
    def _retry_after(exc) -> float | None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("retry-after") or headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None
