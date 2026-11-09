"""Groq Chat Completions adapter for strict JSON Schema proposals."""

import json
import time

from ..errors import LLMProviderError, LLMSchemaViolation, RateLimitExceeded


class GroqProvider:
    def __init__(self, api_key: str, model: str, client=None, sleep=time.sleep):
        if client is None:
            from groq import Groq
            client = Groq(api_key=api_key, max_retries=0)
        self.client = client
        self.model = model
        self.sleep = sleep
        self.request_count = 0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def structured_call(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        for attempt in range(3):
            try:
                self.request_count += 1
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    timeout=timeout,
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
                malformed = isinstance(exc, (json.JSONDecodeError, ValueError, IndexError, AttributeError, TypeError))
                retryable = malformed or any(word in name for word in ("ratelimit", "timeout", "connection"))
                if retryable and attempt < 2:
                    self.sleep(0.25 * (2 ** attempt))
                    continue
                if "ratelimit" in name:
                    raise RateLimitExceeded("Groq rate limit exceeded", code="llm_rate_limited") from None
                if "timeout" in name or "connection" in name:
                    raise LLMProviderError("Groq request failed", code="llm_provider_unavailable") from None
                if malformed:
                    raise LLMSchemaViolation("Groq returned a malformed structured response", code="llm_schema_violation") from None
                raise LLMProviderError("Groq request failed", code="llm_provider_error") from None
