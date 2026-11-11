from concurrent.futures import ThreadPoolExecutor
import asyncio
from decimal import Decimal
import time

import pytest
import httpx
from sqlalchemy import select

from dividend.config import Settings
from dividend.api.deps import plan_access_token
from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.errors import LLMProviderError, RateLimitExceeded
from dividend.llm.groq_provider import GroqProvider
from dividend.llm.limits import GroqRequestBudget
from dividend.models import LLMCache, Maintainer, Plan, PlanLine, Session
from dividend.main import create_app
from dividend.services.approval_proposal import freeze_proposal
from dividend.services.evidence_review_service import EvidenceReviewService


@pytest.fixture
def evidence_db(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'evidence-review.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def make_plan(factory):
    with factory.begin() as db:
        session = Session(mode="upload", status="ready", source_names=["curl"], source_count=1)
        db.add(session)
        db.flush()
        maintainer = Maintainer(upstream_key="github.com/curl/curl", display_name="Demo",
                                email="demo@example.test")
        db.add(maintainer)
        db.flush()
        evidence = [
            {"event_id": 11, "github_user_id": 9001, "signal_type": "merged_pull_request",
             "pr_id": 101, "pr_number": 1, "source_url": "https://github.com/curl/curl/pull/1",
             "timestamp": "2026-04-01T00:00:00Z", "mapped_maintainer_id": maintainer.id},
            {"event_id": 12, "github_user_id": 9001, "signal_type": "code_review",
             "pr_id": 102, "pr_number": 2, "source_url": "https://github.com/curl/curl/pull/2",
             "timestamp": "2026-04-02T00:00:00Z", "mapped_maintainer_id": maintainer.id},
        ]
        proposal = {
            "schema_version": 1,
            "projects": [{"project_id": "github.com/curl/curl", "amount_cents": 100,
                "payable_cents": 100, "reserved_cents": 0,
                "maintainers": [{"maintainer_id": maintainer.id, "amount_cents": 100,
                                 "share_bps": 10000}], "evidence": evidence}],
            "evidence_snapshot": {"snapshot_id": "snapshot-test",
                                  "captured_at": "2026-11-01T00:00:00Z"},
            "identity_mapping": {"mapping_id": "mapping-test", "seeded_demo": True},
            "allocation_policy_version": "policy-v1", "currency": "USD",
        }
        plan = Plan(session_id=session.id, state="draft", budget_cents=100,
                    payable_total_cents=100, reserved_total_cents=0,
                    fee_policy_version="policy-v1",
                    fee_policy_snapshot={"approval_proposal": freeze_proposal(proposal)})
        db.add(plan)
        db.flush()
        db.add(PlanLine(plan_id=plan.id, upstream_key="github.com/curl/curl",
                        display_name="curl", weight=Decimal("1"), amount_cents=100,
                        payable_cents=100, reserved_cents=0, reason="test",
                        facts_snapshot={}, maintainer_splits=[{
                            "maintainer_id": maintainer.id, "display_name": "Demo",
                            "share_bps": 10000, "amount_cents": 100,
                        }]))
        return plan.id


def valid_summary():
    return {"summary": "Contributor merged PR #1 and reviewed PR #2.",
            "contributors": [{"contributor_id": "9001",
                "observation": "Merged PR #1 and reviewed PR #2.", "cited_pr_numbers": [1, 2]}],
            "cited_pr_numbers": [1, 2],
            "advisory_flags": ["merged_pr_activity", "review_activity", "multiple_pr_evidence"]}


class FakeGroq:
    def __init__(self, result=None, error=None, delay=0):
        self.result = result or valid_summary()
        self.error = error
        self.delay = delay
        self.request_count = 0
        self.calls = []

    def structured_call(self, prompt, schema, timeout=30):
        self.request_count += 1
        self.calls.append((prompt, schema, timeout))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def test_cached_summary_and_financial_data_are_unchanged(evidence_db):
    plan_id = make_plan(evidence_db)
    provider = FakeGroq()
    service = EvidenceReviewService(evidence_db, provider)
    with evidence_db() as db:
        plan = db.get(Plan, plan_id)
        snapshot = plan.fee_policy_snapshot
        line = db.scalar(select(PlanLine).where(PlanLine.plan_id == plan_id))
        financials = (plan.state, plan.budget_cents, line.amount_cents,
                      line.payable_cents, line.reserved_cents)
        first = service.review(plan_id, db)
        second = service.review(plan_id, db)
        assert first == second
        assert first["status"] == "available"
        assert provider.request_count == 1
        assert "9001" in provider.calls[0][0]
        assert "100" not in provider.calls[0][0]  # No allocation cents in the prompt.
        assert "email" not in provider.calls[0][0].lower()
        assert plan.fee_policy_snapshot == snapshot
        assert (plan.state, plan.budget_cents, line.amount_cents,
                line.payable_cents, line.reserved_cents) == financials


def test_changed_proposal_hash_misses_cache(evidence_db):
    plan_id = make_plan(evidence_db)
    provider = FakeGroq()
    service = EvidenceReviewService(evidence_db, provider)
    with evidence_db() as db:
        service.review(plan_id, db)
    with evidence_db.begin() as db:
        plan = db.get(Plan, plan_id)
        frozen = plan.fee_policy_snapshot["approval_proposal"]
        proposal = frozen["snapshot"]
        proposal["projects"][0]["evidence"][0]["source_url"] += "?changed=1"
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                    "approval_proposal": freeze_proposal(proposal)}
    with evidence_db() as db:
        assert service.review(plan_id, db)["status"] == "available"
    assert provider.request_count == 2


def test_same_proposal_concurrent_misses_share_one_groq_call(evidence_db):
    plan_id = make_plan(evidence_db)
    provider = FakeGroq(delay=0.05)
    service = EvidenceReviewService(evidence_db, provider)
    def review():
        with evidence_db() as db:
            return service.review(plan_id, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: review(), range(2)))
    assert results[0] == results[1]
    assert provider.request_count == 1


@pytest.mark.parametrize("state", [
    {"requests": [time.time()] * 2, "tokens": []},
    {"requests": [time.time()] * 20, "tokens": []},
    {"requests": [], "tokens": [[time.time(), 7000]]},
    {"requests": [], "tokens": [[time.time(), 198500]]},
])
def test_rpm_rpd_tpm_and_tpd_guards_skip_provider(evidence_db, state):
    budget = GroqRequestBudget(evidence_db)
    with evidence_db.begin() as db:
        db.add(LLMCache(key=budget.KEY, response=state))
    with pytest.raises(RateLimitExceeded):
        budget.reserve(1000)


def test_exhausted_budget_prevents_provider_call(evidence_db):
    budget = GroqRequestBudget(evidence_db)
    with evidence_db.begin() as db:
        db.add(LLMCache(key=budget.KEY, response={
            "requests": [time.time()] * 2, "tokens": [],
        }))
    class Completions:
        calls = 0
        def create(self, **kwargs): self.calls += 1
    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {
        "completions": completions})()})()
    provider = GroqProvider("secret", "model", client, budget=budget)
    with pytest.raises(RateLimitExceeded):
        provider.structured_call("brief", {"type": "object"})
    assert completions.calls == 0


def test_provider_429_respects_retry_after_and_retries_once():
    pauses = []
    rate_limited = type("RateLimitError", (Exception,), {
        "status_code": 429,
        "response": type("Response", (), {"headers": {"retry-after": "7"}})(),
    })
    class Completions:
        def __init__(self): self.calls = 0
        def create(self, **kwargs):
            self.calls += 1
            raise rate_limited("busy")
    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {
        "completions": completions})()})()
    provider = GroqProvider("secret", "openai/gpt-oss-20b", client,
                             sleep=pauses.append, jitter=lambda low, high: 1)
    with pytest.raises(RateLimitExceeded) as error:
        provider.structured_call("brief", {"type": "object"})
    assert pauses == [7.0]
    assert completions.calls == provider.request_count == 2
    assert error.value.retry_after_seconds == 7


def test_permanent_provider_error_is_not_retried():
    unauthorized = type("AuthenticationError", (Exception,), {"status_code": 401})
    class Completions:
        calls = 0
        def create(self, **kwargs):
            self.calls += 1
            raise unauthorized("secret")
    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {
        "completions": completions})()})()
    provider = GroqProvider("secret", "model", client, sleep=lambda _: None)
    with pytest.raises(LLMProviderError):
        provider.structured_call("brief", {"type": "object"})
    assert completions.calls == 1


@pytest.mark.parametrize("status", [400, 401])
def test_permanent_http_errors_are_not_retried(status):
    error = type("BadRequestError", (Exception,), {"status_code": status})
    class Completions:
        calls = 0
        def create(self, **kwargs):
            self.calls += 1
            raise error("secret")
    calls = Completions()
    sdk = type("Client", (), {"chat": type("Chat", (), {"completions": calls})()})()
    provider = GroqProvider("secret", "model", sdk, sleep=lambda _: None)
    with pytest.raises(LLMProviderError):
        provider.structured_call("facts", {"type": "object"})
    assert calls.calls == 1


@pytest.mark.parametrize("change", [
    lambda data: data["contributors"][0].update(cited_pr_numbers=[999]),
    lambda data: data["advisory_flags"].append("docs_only_pattern"),
    lambda data: data.update(summary="Contributor worked on PR #999."),
    lambda data: data.update(summary="Evidence shows a docs-only pattern."),
])
def test_invalid_citations_and_flags_are_rejected_and_not_cached(evidence_db, change):
    plan_id = make_plan(evidence_db)
    output = valid_summary()
    change(output)
    provider = FakeGroq(result=output)
    service = EvidenceReviewService(evidence_db, provider)
    with evidence_db() as db:
        result = service.review(plan_id, db)
        assert result["status"] == "invalid"
    with evidence_db() as db:
        assert service.review(plan_id, db)["status"] == "invalid"
    assert provider.request_count == 2


def test_provider_unavailable_returns_explicit_status(evidence_db):
    plan_id = make_plan(evidence_db)
    service = EvidenceReviewService(evidence_db, FakeGroq(error=LLMProviderError(
        "unavailable", code="llm_provider_unavailable")))
    with evidence_db() as db:
        assert service.review(plan_id, db)["reason"] == "groq_unavailable"


def test_plan_evidence_review_route_is_plan_token_guarded(evidence_db):
    plan_id = make_plan(evidence_db)
    engine = evidence_db.kw["bind"]
    app = create_app(Settings(ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True, GROQ_API_KEY="test-key",
        DEMO_SEED_ON_BOOT=False), engine=engine, session_factory=evidence_db,
        llm_provider=FakeGroq())
    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test") as client:
                path = f"/api/plans/{plan_id}/evidence-review"
                denied = await client.get(path)
                assert denied.status_code == 401
                response = await client.get(path, headers={
                    "X-Plan-Token": plan_access_token("test-token", plan_id),
                })
                assert response.status_code == 200
                assert response.json()["status"] == "available"
    asyncio.run(exercise())


def test_model_default_is_configurable():
    settings = Settings(ADMIN_TOKEN="admin", PUBLIC_BASE_URL="https://example.test",
                        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=False)
    assert settings.LLM_MODEL == "openai/gpt-oss-20b"
    assert Settings(ADMIN_TOKEN="admin", PUBLIC_BASE_URL="https://example.test",
                    PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=False,
                    LLM_MODEL="custom/groq-model").LLM_MODEL == "custom/groq-model"
