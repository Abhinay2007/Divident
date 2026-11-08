import pytest

from dividend.enrichment.base import FactSet
from dividend.errors import RateLimitExceeded
from dividend.llm.limits import LiveResolutionLimits, SessionLimitedProvider


def test_live_limits_cap_unique_sources_per_session_and_calls_per_hour():
    limits = LiveResolutionLimits(max_sources_per_session=2, max_calls_per_hour=1)
    selected = limits.select_sources("session-a", [FactSet(f"pkg-{i}") for i in range(4)])
    assert [fact.source_name for fact in selected] == ["pkg-0", "pkg-1"]
    assert limits.select_sources("session-a", [FactSet("pkg-2")]) == []
    assert [fact.source_name for fact in limits.select_sources("session-b", [FactSet("pkg-2")])] == ["pkg-2"]

    class Provider:
        calls = 0
        def structured_call(self, prompt, schema, timeout=30):
            self.calls += 1
            return {"proposals": []}

    provider = Provider()
    limited = SessionLimitedProvider(provider, limits, "session-a")
    limited.structured_call("facts", {})
    with pytest.raises(RateLimitExceeded, match="hourly limit"):
        limited.structured_call("facts", {})
    assert provider.calls == 1
