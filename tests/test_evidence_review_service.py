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
from dividend.llm.modal_provider import ModalProvider
from dividend.llm.limits import ModalRequestBudget
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


def test_realistic_multi_project_output_validates_all_contributors_and_citations():
    contributors = {
        "123": {"contributor_id": "123", "merged_pr_numbers": [11],
            "reviewed_pr_numbers": [14], "evidence_references": [
                {"pr_number": 11, "url": "https://github.com/alpha/engine/pull/11"},
                {"pr_number": 14, "url": "https://github.com/alpha/engine/pull/14"},
            ]},
        "456": {"contributor_id": "456", "merged_pr_numbers": [],
            "reviewed_pr_numbers": [20], "evidence_references": [
                {"pr_number": 20, "url": "https://github.com/beta/tool/pull/20"},
            ]},
    }
    raw = {
        "summary": "Contributor 123 merged PR #11 and reviewed PR #14; contributor 456 reviewed PR #20.",
        "contributors": [
            {"contributor_id": "123", "observation": "Merged PR #11 and reviewed PR #14.",
             "cited_pr_numbers": [11, 14]},
            {"contributor_id": "456", "observation": "Reviewed PR #20.",
             "cited_pr_numbers": [20]},
        ],
        "cited_pr_numbers": [11, 14, 20],
        "advisory_flags": ["merged_pr_activity", "review_activity", "multiple_pr_evidence"],
    }
    result = EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert result["status"] == "available"
    assert [row["contributor_id"] for row in result["contributors"]] == ["123", "456"]
    assert [row["pr_number"] for row in result["citations"]] == [11, 14, 20]


class FakeModal:
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
    provider = FakeModal()
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
        assert first["citations"] == [
            {"pr_number": 1, "url": "https://github.com/curl/curl/pull/1"},
            {"pr_number": 2, "url": "https://github.com/curl/curl/pull/2"},
        ]
        assert provider.request_count == 1
        assert "9001" in provider.calls[0][0]
        assert '"merged"' in provider.calls[0][0]
        assert ('Supported advisory flags: ["merged_pr_activity","multiple_pr_evidence",'
                '"review_activity"]') in provider.calls[0][0]
        assert "merged_pr_numbers" not in provider.calls[0][0]
        assert "100" not in provider.calls[0][0]  # No allocation cents in the prompt.
        assert "email" not in provider.calls[0][0].lower()
        assert plan.fee_policy_snapshot == snapshot
        assert (plan.state, plan.budget_cents, line.amount_cents,
                line.payable_cents, line.reserved_cents) == financials


def test_oversized_evidence_returns_budget_error_without_modal_call(evidence_db):
    plan_id = make_plan(evidence_db)
    with evidence_db.begin() as db:
        plan = db.get(Plan, plan_id)
        proposal = plan.fee_policy_snapshot["approval_proposal"]["snapshot"]
        proposal["projects"][0]["evidence"][0]["source_url"] += "?context=" + " context for review" * 1500
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                    "approval_proposal": freeze_proposal(proposal)}
    provider = FakeModal()
    with evidence_db() as db:
        result = EvidenceReviewService(evidence_db, provider).review(plan_id, db)
    assert result["status"] == "unavailable"
    assert result["reason"] == "input_token_budget_exceeded"
    assert provider.request_count == 0


def test_changed_proposal_hash_misses_cache(evidence_db):
    plan_id = make_plan(evidence_db)
    provider = FakeModal()
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


def test_same_proposal_concurrent_misses_share_one_modal_call(evidence_db):
    plan_id = make_plan(evidence_db)
    provider = FakeModal(delay=0.05)
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
    budget = ModalRequestBudget(evidence_db)
    with evidence_db.begin() as db:
        db.add(LLMCache(key=budget.KEY, response=state))
    with pytest.raises(RateLimitExceeded):
        budget.reserve(1000)


def test_exhausted_budget_prevents_provider_call(evidence_db):
    budget = ModalRequestBudget(evidence_db)
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
    provider = ModalProvider("model", client, budget=budget)
    with pytest.raises(RateLimitExceeded):
        provider.structured_call("brief", {"type": "object"})
    assert completions.calls == 0


def test_provider_rate_limit_is_reported_without_retry():
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
    provider = ModalProvider("deepseek-ai/DeepSeek-V4.1-Flash-1017", client)
    with pytest.raises(RateLimitExceeded) as error:
        provider.structured_call("brief", {"type": "object"})
    assert completions.calls == provider.request_count == 1
    assert error.value.code == "modal_rate_limited"


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
    provider = ModalProvider("model", client)
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
    provider = ModalProvider("model", sdk)
    with pytest.raises(LLMProviderError):
        provider.structured_call("facts", {"type": "object"})
    assert calls.calls == 1


@pytest.mark.parametrize("change", [
    lambda data: data["contributors"][0].update(cited_pr_numbers=[999]),
    lambda data: data["contributors"][0].update(cited_pr_numbers=[1, 1]),
    lambda data: data["advisory_flags"].append("docs_only_pattern"),
    lambda data: data.update(summary="Contributor worked on PR #999."),
    lambda data: data.update(summary="Evidence shows a docs-only pattern."),
])
def test_invalid_citations_and_flags_are_rejected_and_not_cached(evidence_db, change):
    plan_id = make_plan(evidence_db)
    output = valid_summary()
    change(output)
    provider = FakeModal(result=output)
    service = EvidenceReviewService(evidence_db, provider)
    with evidence_db() as db:
        result = service.review(plan_id, db)
        assert result["status"] == "invalid"
    with evidence_db() as db:
        assert service.review(plan_id, db)["status"] == "invalid"
    assert provider.request_count == 2


@pytest.mark.parametrize(("change", "field", "reason"), [
    (lambda data: data["contributors"][0].update(contributor_id="unknown-id"),
     "contributors[].contributor_id", "unknown_contributor_id"),
    (lambda data: (data["contributors"][0].update(cited_pr_numbers=[999]),
                   data.update(cited_pr_numbers=[999])),
     "contributors[].cited_pr_numbers", "citation_not_in_evidence"),
])
def test_invalid_model_evidence_reports_field_and_reason_without_logging_payload(
        evidence_db, caplog, change, field, reason):
    import logging

    plan_id = make_plan(evidence_db)
    output = valid_summary()
    change(output)
    provider = FakeModal(result=output)
    caplog.set_level(logging.WARNING)
    with evidence_db() as db:
        response = EvidenceReviewService(evidence_db, provider).review(plan_id, db)
    assert response["status"] == "invalid"
    assert f"validation_field={field}" in caplog.text
    assert f"validation_reason={reason}" in caplog.text
    assert "unknown-id" not in caplog.text
    assert "Summarize these GitHub contributions" not in caplog.text


def test_model_json_parse_failure_is_logged_by_field_and_reason(evidence_db, caplog):
    import logging

    plan_id = make_plan(evidence_db)

    class MalformedModal:
        def structured_call(self, *args, **kwargs):
            from dividend.errors import LLMSchemaViolation
            raise LLMSchemaViolation(
                "Malformed structured response", code="llm_schema_violation",
                detail={"field": "content", "reason": "malformed_json"},
            )

    caplog.set_level(logging.WARNING)
    with evidence_db() as db:
        result = EvidenceReviewService(evidence_db, MalformedModal()).review(plan_id, db)
    assert result["status"] == "invalid"
    assert "validation_field=content" in caplog.text
    assert "validation_reason=malformed_json" in caplog.text


def test_provider_unavailable_returns_explicit_status(evidence_db):
    plan_id = make_plan(evidence_db)
    service = EvidenceReviewService(evidence_db, FakeModal(error=LLMProviderError(
        "unavailable", code="modal_endpoint_unavailable")))
    with evidence_db() as db:
        assert service.review(plan_id, db)["reason"] == "modal_endpoint_unavailable"


def test_disabled_modal_provider_returns_explicit_status(evidence_db):
    plan_id = make_plan(evidence_db)
    with evidence_db() as db:
        result = EvidenceReviewService(evidence_db).review(plan_id, db)
    assert result["reason"] == "modal_disabled"


def test_request_budget_failure_logs_safe_diagnostics(evidence_db, caplog):
    import logging

    plan_id = make_plan(evidence_db)
    budget = ModalRequestBudget(evidence_db)
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
    provider = ModalProvider("deepseek-ai/DeepSeek-V4.1-Flash-1017", client, budget=budget)
    caplog.set_level(logging.WARNING)
    with evidence_db() as db:
        result = EvidenceReviewService(evidence_db, provider).review(plan_id, db)
    assert result["reason"] == "llm_request_budget_exhausted"
    assert completions.calls == 0
    assert "guard=requests_per_minute" in caplog.text
    assert "error_type=RateLimitExceeded" in caplog.text
    assert "provider=ModalProvider" in caplog.text
    assert "model=deepseek-ai/DeepSeek-V4.1-Flash-1017" in caplog.text
    assert "input_tokens=" in caplog.text
    assert "secret" not in caplog.text
    assert "Summarize these GitHub contributions" not in caplog.text


def test_cited_pull_request_without_a_verified_github_url_is_rejected(evidence_db):
    plan_id = make_plan(evidence_db)
    with evidence_db.begin() as db:
        plan = db.get(Plan, plan_id)
        proposal = plan.fee_policy_snapshot["approval_proposal"]["snapshot"]
        proposal["projects"][0]["evidence"][0]["source_url"] = "https://evil.example/pull/1"
        plan.fee_policy_snapshot = {**plan.fee_policy_snapshot,
                                    "approval_proposal": freeze_proposal(proposal)}
    service = EvidenceReviewService(evidence_db, FakeModal())
    with evidence_db() as db:
        assert service.review(plan_id, db)["status"] == "invalid"


def test_plan_evidence_review_route_is_plan_token_guarded(evidence_db):
    plan_id = make_plan(evidence_db)
    engine = evidence_db.kw["bind"]
    provider = FakeModal()
    app = create_app(Settings(ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True, MODAL_PROXY_TOKEN_ID="test-id", MODAL_PROXY_TOKEN_SECRET="test-secret",
        DEMO_SEED_ON_BOOT=False), engine=engine, session_factory=evidence_db,
        llm_provider=provider)
    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test") as client:
                path = f"/api/plans/{plan_id}/evidence-review"
                denied = await client.get(path)
                assert denied.status_code == 401
                headers = {"X-Plan-Token": plan_access_token("test-token", plan_id)}
                response = await client.get(path, headers=headers)
                assert response.status_code == 200
                assert response.json()["status"] == "available"
                assert provider.request_count == 1
    asyncio.run(exercise())


def test_app_builds_live_modal_provider_with_existing_request_budget(evidence_db, monkeypatch):
    monkeypatch.setenv("MODAL_ENDPOINT_URL", "https://modal.test/v1")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "test-id")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "test-secret")
    app = create_app(Settings(
        ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True,
        LLM_KILL_SWITCH=False, MODAL_PROXY_TOKEN_ID="test-id",
        MODAL_PROXY_TOKEN_SECRET="test-secret", DEMO_SEED_ON_BOOT=False,
    ), engine=evidence_db.kw["bind"], session_factory=evidence_db)
    try:
        assert isinstance(app.state.llm, ModalProvider)
        assert app.state.evidence_review_service.provider is app.state.llm
        assert app.state.llm.budget is not None
    finally:
        app.state.llm.close()


def test_model_default_is_configurable():
    settings = Settings(ADMIN_TOKEN="admin", PUBLIC_BASE_URL="https://example.test",
                        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=False)
    assert settings.MODAL_MODEL == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    assert Settings(ADMIN_TOKEN="admin", PUBLIC_BASE_URL="https://example.test",
                    PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=False,
                    MODAL_MODEL="custom/modal-model").MODAL_MODEL == "custom/modal-model"
