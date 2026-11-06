from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from dividend.allocation.snapshot import build_facts_snapshot, source_facts_payload


@dataclass
class GitHubFacts:
    full_name: str
    archived: bool
    last_commit: str | None
    committers_12m: int | None
    funding_yml: bool


@dataclass
class FactSet:
    source_name: str
    repository_url: str | None
    homepage: str | None
    donation: str | None
    github: GitHubFacts | None
    partial: bool


def test_original_source_facts_survive_serialization_and_snapshot_construction():
    facts = FactSet("pkg", "https://github.com/Owner/Repo", None, None,
                    GitHubFacts("Owner/Repo", False, "2026-02-03T10:11:12+02:00", 2, True), True)
    persisted = source_facts_payload([facts])
    project = SimpleNamespace(
        source_facts=persisted,
        source_names=["pkg"],
        binary_count=3,
        need_signals={"archived": False, "last_commit": "2026-02-03", "committers_12m": 2,
                      "funding_yml": True, "partial": True},
    )
    resolution = SimpleNamespace(
        source_name="pkg", method="repo_url", upstream_key="owner/repo",
        evidence=[{"field": "udd.repository", "value": "https://github.com/Owner/Repo"}],
        validated=False, cached=False, created_at=datetime(2026, 2, 3, tzinfo=UTC),
    )
    snapshot = build_facts_snapshot(project, [resolution], expected_binary_count=3)
    serialized = snapshot.to_dict()
    assert serialized["sources"][0]["github"]["last_commit"] == "2026-02-03T10:11:12+02:00"
    assert serialized["normalized"]["last_commit"] == date(2026, 2, 3).isoformat()
    assert serialized["sources"][0]["resolution"]["created_at"] == "2026-02-03T00:00:00Z"


def test_snapshot_builder_rejects_missing_legacy_source_facts_and_mismatches():
    base = SimpleNamespace(source_facts=None, source_names=["pkg"], binary_count=1,
                           need_signals={"archived": None, "last_commit": None, "committers_12m": None,
                                         "funding_yml": None, "partial": False})
    with pytest.raises(ValueError, match="unavailable"):
        build_facts_snapshot(base, [])
    base.source_facts = {"other": {}}
    with pytest.raises(ValueError, match="match project source_names"):
        build_facts_snapshot(base, [])
