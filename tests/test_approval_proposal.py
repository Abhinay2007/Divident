from copy import deepcopy

import pytest

from dividend.services.approval_proposal import freeze_proposal, proposal_sha256


def proposal():
    return {
        "schema_version": 1,
        "projects": [{
            "project_id": "github.com/curl/curl", "amount_cents": 1000,
            "payable_cents": 700, "reserved_cents": 300,
            "maintainers": [{"maintainer_id": "maintainer-1", "amount_cents": 700,
                             "share_bps": 10000}],
            "evidence": [{"event_id": 4, "pr_id": 2, "pr_number": 1,
                          "source_url": "https://github.com/curl/curl/pull/1"}],
        }],
        "evidence_snapshot": {"snapshot_id": "snapshot-1", "captured_at": "2026-01-01T00:00:00Z"},
        "identity_mapping": {"mapping_id": "mapping-1", "seeded_demo": True},
        "allocation_policy_version": "policy-1", "currency": "USD",
    }


def test_proposal_hash_is_canonical_and_repeats_for_identical_data():
    first = proposal()
    reordered = dict(reversed(list(first.items())))
    assert proposal_sha256(first) == proposal_sha256(reordered)
    assert freeze_proposal(first)["sha256"] == proposal_sha256(first)


@pytest.mark.parametrize("change", [
    lambda data: data["projects"][0].update(amount_cents=1001),
    lambda data: data["projects"][0]["evidence"][0].update(
        source_url="https://github.com/curl/curl/pull/2"),
    lambda data: data.update(allocation_policy_version="policy-2"),
    lambda data: data["identity_mapping"].update(seeded_demo=False),
])
def test_proposal_hash_changes_when_decision_inputs_change(change):
    before = proposal()
    after = deepcopy(before)
    change(after)
    assert proposal_sha256(before) != proposal_sha256(after)
