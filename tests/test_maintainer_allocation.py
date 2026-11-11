from dividend.allocation.maintainer import SERVER_EVIDENCE_PROVENANCE, distribute
from pydantic import ValidationError
import pytest
from dividend.schemas import PlanCreateRequest


def test_server_validated_mixed_evidence_produces_deterministic_dynamic_shares_and_exact_cents():
    people = [{"id": "a", "display_name": "A", "opted_in": True},
              {"id": "b", "display_name": "B", "opted_in": True}]
    evidence = [
        {"maintainer_id": "a", "signal_type": "merged_pull_request", "reference": "https://example.test/pr/1", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "a", "signal_type": "release_contribution", "reference": "https://example.test/release/1", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "b", "signal_type": "accepted_commit", "reference": "https://example.test/commit/2", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "b", "signal_type": "resolved_issue", "reference": "https://example.test/issue/3", "provenance": SERVER_EVIDENCE_PROVENANCE},
    ]
    first = distribute(101, people, evidence)
    assert first == distribute(101, people, evidence)
    assert [item["amount_cents"] for item in first["maintainers"]] == [56, 45]
    assert sum(item["amount_cents"] for item in first["maintainers"]) + first["reserved_cents"] == 101
    assert [item["score"] for item in first["maintainers"]] == [5, 4]


def test_unverified_or_single_signal_evidence_reserves_the_entire_amount():
    people = [{"id": "a", "display_name": "A", "opted_in": True}]
    evidence = [{"maintainer_id": "a", "signal_type": "accepted_commit",
                 "reference": "https://example.test/commit/1", "provenance": SERVER_EVIDENCE_PROVENANCE}]
    result = distribute(99, people, evidence)
    assert result["maintainers"] == []
    assert result["reserved_cents"] == 99


def test_unmapped_and_insufficient_evidence_are_reserved_and_cent_allocation_is_deterministic():
    people = [{"id": "a", "display_name": "A", "opted_in": True},
              {"id": "b", "display_name": "B", "opted_in": True}]
    evidence = [
        {"maintainer_id": "a", "signal_type": "merged_pull_request", "reference": "https://github.com/curl/curl/pull/1", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "a", "signal_type": "code_review", "reference": "https://github.com/curl/curl/pull/2", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": None, "signal_type": "code_review", "reference": "https://github.com/curl/curl/pull/3", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "b", "signal_type": "code_review", "reference": "https://github.com/curl/curl/pull/4", "provenance": SERVER_EVIDENCE_PROVENANCE},
    ]
    first = distribute(101, people, evidence)
    assert first == distribute(101, people, evidence)
    assert first["maintainers"][0]["amount_cents"] == 67
    assert first["reserved_cents"] == 34
    assert first["maintainers"][0]["share_bps"] == 10000
    assert sum(row["amount_cents"] for row in first["maintainers"]) + first["reserved_cents"] == 101


def test_multiple_mapped_maintainers_receive_evidence_weighted_exact_cents():
    people = [{"id": "a", "display_name": "A", "opted_in": True},
              {"id": "b", "display_name": "B", "opted_in": True}]
    evidence = [
        {"maintainer_id": mid, "signal_type": signal,
         "reference": f"https://github.com/curl/curl/pull/{index}",
         "provenance": SERVER_EVIDENCE_PROVENANCE}
        for mid, signal, index in [
            ("a", "merged_pull_request", 1), ("a", "code_review", 2),
            ("b", "merged_pull_request", 3), ("b", "code_review", 4),
        ]
    ]
    result = distribute(101, people, evidence)
    assert [row["amount_cents"] for row in result["maintainers"]] == [51, 50]
    assert result["reserved_cents"] == 0
    assert sum(row["amount_cents"] for row in result["maintainers"]) == 101


def test_unverified_or_invalid_evidence_never_supports_a_payout():
    people = [{"id": "a", "display_name": "A", "opted_in": True}]
    rows = [
        {"maintainer_id": "a", "signal_type": "merged_pull_request", "reference": "https://github.com/curl/curl/pull/1", "verified": True},
        {"maintainer_id": "a", "signal_type": "code_review", "reference": "http://github.com/curl/curl/pull/2", "provenance": SERVER_EVIDENCE_PROVENANCE},
        {"maintainer_id": "a", "signal_type": "merged_pull_request", "reference": "https://github.com/curl/curl/pull/3", "provenance": SERVER_EVIDENCE_PROVENANCE},
    ]
    result = distribute(99, people, rows)
    assert result["maintainers"] == []
    assert result["reserved_cents"] == 99


def test_client_style_verified_flag_is_not_scorer_provenance():
    person = [{"id": "a", "display_name": "A", "opted_in": True}]
    evidence = [
        {"maintainer_id": "a", "signal_type": signal,
         "reference": f"https://github.com/curl/curl/pull/{index}", "verified": True}
        for index, signal in [(1, "merged_pull_request"), (2, "code_review")]
    ]
    result = distribute(99, person, evidence)
    assert result["maintainers"] == []
    assert result["reserved_cents"] == 99


@pytest.mark.parametrize("extra", [
    {"evidence": [{"signal_type": "merged_pull_request", "verified": True}]},
    {"verified": True},
])
def test_plan_request_rejects_client_evidence_and_verified_flags(extra):
    with pytest.raises(ValidationError):
        PlanCreateRequest.model_validate({
            "budget_cents": 1000, "evaluation_date": "2026-11-06", **extra,
        })
