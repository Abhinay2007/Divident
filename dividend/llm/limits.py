"""In-process caps for the hosted demo's bounded live resolver."""

from collections import deque
from threading import Lock
from time import monotonic

from ..errors import RateLimitExceeded


class LiveResolutionLimits:
    def __init__(self, max_sources_per_session: int = 5, max_calls_per_hour: int = 100):
        self.max_sources_per_session = max_sources_per_session
        self.max_calls_per_hour = max_calls_per_hour
        self._sources: dict[str, set[str]] = {}
        self._calls = deque()
        self._lock = Lock()

    def select_sources(self, session_id: str, facts: list):
        with self._lock:
            seen = self._sources.setdefault(session_id, set())
            slots = max(0, self.max_sources_per_session - len(seen))
            selected = [fact for fact in facts if fact.source_name not in seen][:slots]
            seen.update(fact.source_name for fact in selected)
            return selected

    def call(self, session_id: str, provider, prompt: str, schema: dict, timeout: int):
        now = monotonic()
        with self._lock:
            while self._calls and self._calls[0] <= now - 3600:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls_per_hour:
                raise RateLimitExceeded("Live resolution hourly limit reached", code="llm_hourly_limit")
            self._calls.append(now)
        return provider.structured_call(prompt, schema, timeout)


class SessionLimitedProvider:
    def __init__(self, provider, limits: LiveResolutionLimits, session_id: str):
        self.provider = provider
        self.limits = limits
        self.session_id = session_id

    def structured_call(self, prompt: str, schema: dict, timeout: int = 30):
        return self.limits.call(self.session_id, self.provider, prompt, schema, timeout)
