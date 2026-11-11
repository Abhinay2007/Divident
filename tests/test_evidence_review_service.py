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


def summary_validation_inputs(summary):
    raw = valid_summary()
    raw["summary"] = summary
    contributors = {"9001": {
        "contributor_id": "9001", "merged_pr_numbers": [1], "reviewed_pr_numbers": [2],
        "evidence_references": [
            {"signal": "merged_pull_request", "pr_number": 1,
             "url": "https://github.com/curl/curl/pull/1"},
            {"signal": "code_review", "pr_number": 2,
             "url": "https://github.com/curl/curl/pull/2"},
        ],
    }}
    return raw, contributors


def test_natural_donor_summary_satisfies_existing_summary_contract():
    summary = ("The supplied evidence shows merged contributions alongside code reviews. "
               "Reviews are a separate form of participation, while these counts alone do not establish impact.")
    raw, contributors = summary_validation_inputs(summary)
    result = EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert result["summary"] == summary


def test_summary_between_500_and_1500_characters_is_valid():
    summary = "The supplied evidence shows merged contributions and code review activity. " + "Further context. " * 40
    assert 500 < len(summary) <= 1500
    raw, contributors = summary_validation_inputs(summary)
    assert EvidenceReviewService._validate(raw, contributors, "proposal-hash")["summary"] == summary


@pytest.mark.parametrize(("summary", "expected_type", "expected_chars", "constraint"), [
    ("", "str", 0, "empty_or_whitespace"),
    (" \t\n", "str", 3, "empty_or_whitespace"),
    ("x" * 1501, "str", 1501, "exceeds_max_length"),
    (None, "NoneType", None, "expected_string"),
    (["text"], "list", None, "expected_string"),
])
def test_summary_boundary_failures_have_safe_metadata(summary, expected_type,
                                                       expected_chars, constraint):
    from dividend.services.evidence_review_service import EvidenceValidationError

    raw, contributors = summary_validation_inputs(summary)
    with pytest.raises(EvidenceValidationError) as error:
        EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert error.value.detail == {
        "field": "summary", "reason": "invalid_text_or_length",
        "summary_type": expected_type, "summary_chars": expected_chars,
        "summary_constraint": constraint,
    }


def test_realistic_multi_project_output_validates_all_contributors_and_citations():
    contributors = {
        "123": {"contributor_id": "123", "merged_pr_numbers": [11],
            "reviewed_pr_numbers": [14], "evidence_references": [
                {"signal": "merged_pull_request", "pr_number": 11,
                 "url": "https://github.com/alpha/engine/pull/11"},
                {"signal": "code_review", "pr_number": 14,
                 "url": "https://github.com/alpha/engine/pull/14"},
            ]},
        "456": {"contributor_id": "456", "merged_pr_numbers": [],
            "reviewed_pr_numbers": [20], "evidence_references": [
                {"signal": "code_review", "pr_number": 20,
                 "url": "https://github.com/beta/tool/pull/20"},
            ]},
        "789": {"contributor_id": "789", "merged_pr_numbers": [31],
            "reviewed_pr_numbers": [], "evidence_references": [
                {"signal": "merged_pull_request", "pr_number": 31,
                 "url": "https://github.com/gamma/lib/pull/31"},
            ]},
    }
    raw = {
        "summary": "The supplied evidence shows merged work and reviews across several projects. "
                   "Review activity is meaningful participation, but these counts do not establish code quality or impact.",
        "contributors": [
            {"contributor_id": "123", "observation": "Activity spans merged work and reviews across two distinct PRs.",
             "cited_pr_numbers": [11, 14]},
            {"contributor_id": "456", "observation": "The supplied record shows review participation on one distinct PR, with no merged PR listed.",
             "cited_pr_numbers": [20]},
            {"contributor_id": "789", "observation": "One verified merged PR appears in the supplied evidence.",
             "cited_pr_numbers": [31]},
        ],
        "cited_pr_numbers": [11, 14, 20, 31],
        "advisory_flags": ["merged_pr_activity", "review_activity", "multiple_pr_evidence"],
    }
    result = EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert result["status"] == "available"
    assert [row["contributor_id"] for row in result["contributors"]] == ["123", "456", "789"]
    assert [(row["contributor_id"], row["activity_type"], row["pr_number"])
            for row in result["citations"]] == [
                ("123", "code_review", 14), ("123", "merged_pull_request", 11),
                ("456", "code_review", 20), ("789", "merged_pull_request", 31),
            ]
    assert [(row["merged_pr_count"], row["reviewed_pr_count"])
            for row in result["contributors"]] == [(1, 1), (0, 1), (1, 0)]


def test_review_events_on_one_pr_count_as_one_distinct_reviewed_pr():
    proposal = {"projects": [{"evidence": [
        {"github_user_id": 456, "pr_number": 20, "signal_type": "code_review",
         "source_url": "https://github.com/beta/tool/pull/20", "event_id": 1},
        {"github_user_id": 456, "pr_number": 20, "signal_type": "code_review",
         "source_url": "https://github.com/beta/tool/pull/20", "event_id": 2},
    ]}]}
    contributor = EvidenceReviewService._aggregate(proposal)["456"]
    assert contributor["merged_pr_numbers"] == []
    assert contributor["reviewed_pr_numbers"] == [20]


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
            {"contributor_id": "9001", "activity_type": "code_review",
             "pr_number": 2, "url": "https://github.com/curl/curl/pull/2"},
            {"contributor_id": "9001", "activity_type": "merged_pull_request",
             "pr_number": 1, "url": "https://github.com/curl/curl/pull/1"},
        ]
        assert first["contributors"][0]["merged_pr_count"] == 1
        assert first["contributors"][0]["reviewed_pr_count"] == 1
        assert provider.request_count == 1
        assert "9001" in provider.calls[0][0]
        assert '"merged"' in provider.calls[0][0]
        assert ('evidence-supported set: ["merged_pr_activity","multiple_pr_evidence",'
                '"review_activity"]') in provider.calls[0][0]
        assert "2–4 concise sentences as an executive summary" in provider.calls[0][0]
        assert "Target approximately 700–1100 characters including spaces and never exceed 1500 characters" in provider.calls[0][0]
        assert "exact distinct merged-PR and reviewed-PR counts are shown separately" in provider.calls[0][0]
        assert "Never invent names" in provider.calls[0][0]
        assert "Do not print PR lists in prose" in provider.calls[0][0]
        assert "merged_pr_numbers" not in provider.calls[0][0]
        assert "amount_cents" not in provider.calls[0][0]
        assert "email" not in provider.calls[0][0].lower()
        assert provider.calls[0][1]["properties"]["summary"] == {
            "type": "string", "minLength": 1, "maxLength": 1500,
        }
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


def test_wrong_contributor_citation_reports_attribution_mismatch():
    from dividend.services.evidence_review_service import EvidenceValidationError

    raw, contributors = summary_validation_inputs("Verified activity includes merged work and review participation.")
    contributors["456"] = {
        "contributor_id": "456", "merged_pr_numbers": [], "reviewed_pr_numbers": [3],
        "evidence_references": [{"signal": "code_review", "pr_number": 3,
                                 "url": "https://github.com/curl/curl/pull/3"}],
    }
    raw["contributors"].append({"contributor_id": "456", "observation": "Reviewed PR #1.",
                                "cited_pr_numbers": [1]})
    raw["cited_pr_numbers"] = [1, 2]
    with pytest.raises(EvidenceValidationError) as error:
        EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert error.value.detail == {
        "field": "contributors[].cited_pr_numbers", "reason": "citation_not_in_evidence",
        "citation_count": 1, "invalid_citation_count": 1, "attribution_mismatch": True,
    }


def test_top_level_citation_set_must_match_findings_and_reports_safe_counts():
    from dividend.services.evidence_review_service import EvidenceValidationError

    raw, contributors = summary_validation_inputs("A concise, evidence-grounded contribution summary.")
    raw["cited_pr_numbers"] = [1]
    with pytest.raises(EvidenceValidationError) as error:
        EvidenceReviewService._validate(raw, contributors, "proposal-hash")
    assert error.value.detail == {
        "field": "cited_pr_numbers", "reason": "citations_do_not_match_findings",
        "citation_count": 1, "expected_citation_count": 2,
        "missing_citation_count": 1, "extra_citation_count": 0,
    }


def test_citation_mismatch_diagnostic_logs_counts_only(evidence_db, caplog):
    import logging

    plan_id = make_plan(evidence_db)
    output = valid_summary()
    output["cited_pr_numbers"] = [1]
    caplog.set_level(logging.WARNING)
    with evidence_db() as db:
        result = EvidenceReviewService(evidence_db, FakeModal(result=output)).review(plan_id, db)
    assert result["status"] == "invalid"
    assert "validation_field=cited_pr_numbers" in caplog.text
    assert "validation_reason=citations_do_not_match_findings" in caplog.text
    assert "missing_citation_count': 1" in caplog.text
    assert "extra_citation_count': 0" in caplog.text
    assert "https://github.com" not in caplog.text
    assert "Merged PR #1" not in caplog.text


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


def test_overlong_summary_diagnostic_does_not_log_summary_text(evidence_db, caplog):
    import logging

    plan_id = make_plan(evidence_db)
    private_summary = "PRIVATE SUMMARY " + "x" * 1501
    result = valid_summary()
    result["summary"] = private_summary
    caplog.set_level(logging.WARNING)
    with evidence_db() as db:
        response = EvidenceReviewService(evidence_db, FakeModal(result=result)).review(plan_id, db)
    assert response["status"] == "invalid"
    assert "validation_field=summary" in caplog.text
    assert "summary_type=str" in caplog.text
    assert f"summary_chars={len(private_summary)}" in caplog.text
    assert "expected_summary_min_non_whitespace_chars=1" in caplog.text
    assert "expected_summary_max_chars=1500" in caplog.text
    assert "summary_constraint=exceeds_max_length" in caplog.text
    assert private_summary not in caplog.text


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
