"""In-process caps for the hosted demo's bounded live resolver."""

from collections import deque
from datetime import UTC, datetime, time
import hashlib
import logging
from threading import Lock
from time import monotonic

from ..errors import RateLimitExceeded
from ..models import LLMCache

_logger = logging.getLogger(__name__)


class ModalRequestBudget:
    """Persistent rolling request/token budgets stored in the existing LLM cache."""

    KEY = hashlib.sha256(b"modal-application-request-budget-v1").hexdigest()
    # shortcut: counter writes serialize per process; use a database lease before multi-worker deployments.
    _lock = Lock()

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def reserve(self, input_tokens: int) -> None:
        if input_tokens > 3000:
            _logger.warning(
                "Modal request budget rejected guard=input_tokens input_tokens=%d limit=3000",
                input_tokens,
            )
            raise RateLimitExceeded("Modal input token limit reached",
                                    code="llm_input_budget_exceeded")
        current = datetime.now(UTC)
        now = current.timestamp()
        day_start = datetime.combine(current.date(), time.min, UTC).timestamp()
        with self._lock, self.session_factory() as db:
            row = db.get(LLMCache, self.KEY)
            state = row.response if row is not None else {"requests": [], "tokens": []}
            requests = [stamp for stamp in state.get("requests", []) if stamp >= day_start]
            tokens = [event for event in state.get("tokens", []) if event[0] >= day_start]
            per_minute_requests = sum(stamp >= now - 60 for stamp in requests)
            per_minute_tokens = sum(amount for stamp, amount in tokens if stamp >= now - 60)
            per_day_tokens = sum(amount for _, amount in tokens)
            request_tokens = input_tokens + 700
            if per_minute_requests >= 2:
                _logger.warning(
                "Modal request budget rejected guard=requests_per_minute "
                    "requests=%d limit=2 input_tokens=%d",
                    per_minute_requests, input_tokens,
                )
                raise RateLimitExceeded("Modal request budget exhausted",
                                        code="llm_request_budget_exhausted")
            if len(requests) >= 20:
                _logger.warning(
                "Modal request budget rejected guard=requests_utc_day "
                    "requests=%d limit=20 input_tokens=%d",
                    len(requests), input_tokens,
                )
                raise RateLimitExceeded("Modal request budget exhausted",
                                        code="llm_request_budget_exhausted")
            if per_minute_tokens + request_tokens > 8000:
                _logger.warning(
                "Modal request budget rejected guard=tokens_per_minute "
                    "tokens=%d requested_tokens=%d limit=8000 input_tokens=%d",
                    per_minute_tokens, request_tokens, input_tokens,
                )
                raise RateLimitExceeded("Modal token budget exhausted",
                                        code="llm_token_budget_exhausted")
            if per_day_tokens + request_tokens > 200_000:
                _logger.warning(
                "Modal request budget rejected guard=tokens_utc_day "
                    "tokens=%d requested_tokens=%d limit=200000 input_tokens=%d",
                    per_day_tokens, request_tokens, input_tokens,
                )
                raise RateLimitExceeded("Modal token budget exhausted",
                                        code="llm_token_budget_exhausted")
            requests.append(now)
            tokens.append([now, request_tokens])
            value = {"requests": requests, "tokens": tokens}
            if row is None:
                db.add(LLMCache(key=self.KEY, response=value))
            else:
                row.response = value
            db.commit()


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
