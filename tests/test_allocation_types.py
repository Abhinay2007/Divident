from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timezone

import pytest

from dividend.allocation import (
    EvidenceItem, FactsSnapshot, GitHubFactsSnapshot, NormalizedFactsSnapshot,
    ProjectFacts, ProjectInput, ResolutionSnapshot, SourceFactsSnapshot,
)


def snapshot(*, binary_count=2, archived=None, last_commit=None, committers_12m=None, partial=True, funding_yml=None):
    return FactsSnapshot(
        sources=(SourceFactsSnapshot(
            source_name="pkg", repository_url=None, homepage=None, donation=None,
            github=None, partial=partial,
            resolution=ResolutionSnapshot("none", None, None, False, False, datetime(2026, 1, 1, tzinfo=UTC)),
        ),),
        normalized=NormalizedFactsSnapshot(binary_count, archived, last_commit, committers_12m, partial, funding_yml),
    )


def project_facts(**overrides):
    values = {
        "archived": None,
        "last_commit": None,
        "committers_12m": None,
        "partial": True,
        "funding_yml": None,
        "snapshot": None,
    }
    values.update(overrides)
    values["snapshot"] = snapshot(
        archived=values["archived"], last_commit=values["last_commit"],
        committers_12m=values["committers_12m"], partial=values["partial"],
        funding_yml=values["funding_yml"],
    ) if values["snapshot"] is None else values["snapshot"]
    return ProjectFacts(**values)


def project_input(**overrides):
    values = {
        "upstream_key": "owner/repo",
        "display_name": "Project",
        "binary_count": 2,
        "facts": project_facts(),
        "opted_in": False,
    }
    values.update(overrides)
    return ProjectInput(**values)


def test_domain_types_construct_and_preserve_unknown_vs_known_values():
    unknown = project_facts()
    known_false_zero = project_facts(archived=False, committers_12m=0, funding_yml=False)
    project = project_input(facts=known_false_zero)

    assert project.upstream_key == "owner/repo"
    assert unknown.archived is None and unknown.committers_12m is None
    assert known_false_zero.archived is False and known_false_zero.committers_12m == 0
    assert known_false_zero.funding_yml is False


def test_domain_types_are_frozen_and_slotted():
    project = project_input()
    with pytest.raises(FrozenInstanceError):
        project.binary_count = 3
    with pytest.raises(FrozenInstanceError):
        project.facts.partial = False
    assert not hasattr(project, "__dict__")


@pytest.mark.parametrize("field", ["upstream_key", "display_name"])
@pytest.mark.parametrize("value", ["", "   ", " padded "])
def test_identifiers_and_display_names_must_be_nonempty_and_trimmed(field, value):
    with pytest.raises(ValueError):
        project_input(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [("upstream_key", "k" * 257), ("display_name", "n" * 201)],
)
def test_identifiers_and_names_enforce_length_limits(field, value):
    with pytest.raises(ValueError, match="at most"):
        project_input(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "2"])
def test_binary_count_requires_strict_positive_integer(value):
    with pytest.raises((TypeError, ValueError)):
        project_input(binary_count=value)


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_committer_count_rejects_non_integer_values(value):
    with pytest.raises(TypeError):
        project_facts(committers_12m=value)


@pytest.mark.parametrize("value", [0, 1, None])
def test_committer_count_accepts_nonnegative_integer_or_none(value):
    assert project_facts(committers_12m=value).committers_12m == value


def test_committer_count_rejects_negative_integer():
    with pytest.raises(ValueError, match="nonnegative"):
        project_facts(committers_12m=-1)


@pytest.mark.parametrize("field", ["archived", "partial", "funding_yml", "opted_in"])
@pytest.mark.parametrize("value", [0, 1, "false", []])
def test_boolean_fields_reject_non_booleans(field, value):
    if field == "opted_in":
        with pytest.raises(TypeError):
            project_input(opted_in=value)
    else:
        with pytest.raises(TypeError):
            project_facts(**{field: value})


@pytest.mark.parametrize("value", ["2026-01-01", datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone.utc), 1, True])
def test_last_commit_requires_date_not_datetime_or_coercible_value(value):
    with pytest.raises(TypeError, match="date or None"):
        project_facts(last_commit=value)


def test_last_commit_accepts_exact_date():
    assert project_facts(last_commit=date(2026, 1, 1)).last_commit == date(2026, 1, 1)


def test_project_input_requires_project_facts_instance():
    with pytest.raises(TypeError, match="ProjectFacts"):
        project_input(facts={"archived": False})


def test_snapshot_records_are_frozen_slotted_and_nested_immutable():
    value = snapshot()
    with pytest.raises(FrozenInstanceError):
        value.sources = ()
    with pytest.raises(FrozenInstanceError):
        value.sources[0].partial = False
    assert not hasattr(value, "__dict__")
    assert isinstance(value.sources, tuple)


def test_snapshot_rejects_duplicate_source_names_and_orders_sources():
    source = snapshot().sources[0]
    with pytest.raises(ValueError, match="unique"):
        FactsSnapshot((source, source), NormalizedFactsSnapshot(2, None, None, None, True, None))
    other = SourceFactsSnapshot("aaa", None, None, None, None, False,
                                ResolutionSnapshot("none", None, None, False, False, datetime(2026, 1, 1, tzinfo=UTC)))
    result = FactsSnapshot((source, other), NormalizedFactsSnapshot(2, None, None, None, True, None))
    assert [item.source_name for item in result.sources] == ["aaa", "pkg"]


def test_evidence_order_and_canonical_json_stability():
    items = (EvidenceItem("z", "2"), EvidenceItem("a", "1"))
    resolution = ResolutionSnapshot("llm", "owner/repo", items, True, False, datetime(2026, 1, 1, tzinfo=UTC))
    assert [(item.field, item.value) for item in resolution.evidence] == [("a", "1"), ("z", "2")]
    result = FactsSnapshot((SourceFactsSnapshot("pkg", None, None, None, None, True, resolution),),
                           NormalizedFactsSnapshot(2, None, None, None, True, None))
    assert result.to_json() == result.to_json()
    assert '"created_at":"2026-01-01T00:00:00Z"' in result.to_json()
    assert '"last_commit":null' in result.to_json()


@pytest.mark.parametrize("timestamp", [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(timezone.utc)])
def test_resolution_timestamp_must_be_aware_utc(timestamp):
    if timestamp.tzinfo is None:
        with pytest.raises(TypeError):
            ResolutionSnapshot("none", None, None, False, False, timestamp)


def test_snapshot_rejects_unsupported_objects_and_normalized_mismatches():
    with pytest.raises((TypeError, ValueError)):
        SourceFactsSnapshot("pkg", object(), None, None, None, True,
                            ResolutionSnapshot("none", None, None, False, False, datetime(2026, 1, 1, tzinfo=UTC)))
    facts = project_facts()
    with pytest.raises(ValueError, match="match ProjectFacts"):
        ProjectFacts(None, None, None, False, None, facts.snapshot)
    with pytest.raises(ValueError, match="binary_count"):
        project_input(binary_count=3)


def test_github_snapshot_strict_documented_values():
    record = GitHubFactsSnapshot("owner/repo", False, "2026-01-01T00:00:00Z", 1, True)
    assert record.last_commit == "2026-01-01T00:00:00Z"
    with pytest.raises(TypeError):
        GitHubFactsSnapshot("owner/repo", 0, None, None, True)
