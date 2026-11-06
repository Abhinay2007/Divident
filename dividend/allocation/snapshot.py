"""Typed construction of audit snapshots from persisted resolution records."""

from datetime import date, datetime
from typing import Iterable, Sequence

from .types import (
    EvidenceItem, FactsSnapshot, GitHubFactsSnapshot, NormalizedFactsSnapshot,
    ResolutionSnapshot, SourceFactsSnapshot,
)


_SOURCE_FIELDS = {"source_name", "repository_url", "homepage", "donation", "github", "partial"}
_GITHUB_FIELDS = {"full_name", "archived", "last_commit", "committers_12m", "funding_yml"}


def source_facts_payload(fact_sets: Iterable[object]) -> dict[str, dict[str, object]]:
    """Return the documented original FactSet fields keyed by source name."""
    result: dict[str, dict[str, object]] = {}
    for fact in fact_sets:
        source_name = getattr(fact, "source_name", None)
        if type(source_name) is not str or not source_name:
            raise TypeError("FactSet.source_name must be a nonempty string")
        if source_name in result:
            raise ValueError(f"duplicate FactSet source_name {source_name!r}")
        github = getattr(fact, "github", None)
        if github is None:
            github_payload = None
        else:
            github_payload = {
                "full_name": github.full_name,
                "archived": github.archived,
                "last_commit": github.last_commit,
                "committers_12m": github.committers_12m,
                "funding_yml": github.funding_yml,
            }
            # Reuse the strict documented GitHub field validation.
            GitHubFactsSnapshot(**github_payload)
        source = {
            "source_name": source_name,
            "repository_url": fact.repository_url,
            "homepage": fact.homepage,
            "donation": fact.donation,
            "github": github_payload,
            "partial": fact.partial,
        }
        for field in ("repository_url", "homepage", "donation"):
            value = source[field]
            if value is not None and type(value) is not str:
                raise TypeError(f"FactSet.{field} must be a string or None")
        if type(source["partial"]) is not bool:
            raise TypeError("FactSet.partial must be a bool")
        result[source_name] = source
    return dict(sorted(result.items()))


def build_facts_snapshot(
    project: object,
    resolutions: Sequence[object],
    *,
    expected_binary_count: int | None = None,
) -> FactsSnapshot:
    """Build a snapshot from UpstreamProject and its SourceResolution rows.

    This helper intentionally refuses legacy rows without persisted source
    facts; it never reconstructs or fabricates original enrichment values.
    """
    source_facts = getattr(project, "source_facts", None)
    if type(source_facts) is not dict:
        raise ValueError("persisted source facts are unavailable")
    source_names = getattr(project, "source_names", None)
    if type(source_names) is not list or any(type(name) is not str for name in source_names):
        raise TypeError("project source_names must be a list of strings")
    if set(source_facts) != set(source_names) or len(source_facts) != len(source_names):
        raise ValueError("persisted source facts must match project source_names")
    resolution_by_name = {getattr(row, "source_name", None): row for row in resolutions}
    if set(resolution_by_name) != set(source_names) or len(resolution_by_name) != len(resolutions):
        raise ValueError("source resolutions must match project source_names exactly")

    sources: list[SourceFactsSnapshot] = []
    for source_name in sorted(source_names):
        raw = source_facts[source_name]
        if type(raw) is not dict or set(raw) != _SOURCE_FIELDS:
            raise ValueError(f"source facts for {source_name!r} do not match the FactSet contract")
        if raw["source_name"] != source_name:
            raise ValueError("source facts key must match FactSet.source_name")
        github_raw = raw["github"]
        github = None
        if github_raw is not None:
            if type(github_raw) is not dict or set(github_raw) != _GITHUB_FIELDS:
                raise ValueError(f"GitHub facts for {source_name!r} do not match the GitHubFacts contract")
            github = GitHubFactsSnapshot(**github_raw)
        row = resolution_by_name[source_name]
        raw_evidence = getattr(row, "evidence", None)
        evidence = None
        if raw_evidence is not None:
            if type(raw_evidence) is not list:
                raise TypeError("resolution evidence must be a list or None")
            evidence = tuple(EvidenceItem(**item) for item in raw_evidence)
        resolution = ResolutionSnapshot(
            method=row.method,
            upstream_key=row.upstream_key,
            evidence=evidence,
            validated=row.validated,
            cached=row.cached,
            created_at=row.created_at,
        )
        sources.append(SourceFactsSnapshot(
            source_name=source_name,
            repository_url=raw["repository_url"],
            homepage=raw["homepage"],
            donation=raw["donation"],
            github=github,
            partial=raw["partial"],
            resolution=resolution,
        ))

    signals = getattr(project, "need_signals", None)
    if type(signals) is not dict or set(signals) != {"archived", "last_commit", "committers_12m", "funding_yml", "partial"}:
        raise ValueError("project need_signals do not match the normalized facts contract")
    normalized_date = signals["last_commit"]
    if normalized_date is not None:
        if type(normalized_date) is not str:
            raise TypeError("persisted normalized last_commit must be an ISO date string or None")
        normalized_date = date.fromisoformat(normalized_date)
    binary_count = project.binary_count
    if expected_binary_count is not None and binary_count != expected_binary_count:
        raise ValueError("snapshot binary_count must match ProjectInput.binary_count")
    return FactsSnapshot(
        sources=tuple(sources),
        normalized=NormalizedFactsSnapshot(
            binary_count=binary_count,
            archived=signals["archived"],
            last_commit=normalized_date,
            committers_12m=signals["committers_12m"],
            partial=signals["partial"],
            funding_yml=signals["funding_yml"],
        ),
    )


def facts_snapshot_from_dict(value: object) -> FactsSnapshot:
    """Reconstruct a typed immutable snapshot from its canonical JSON object."""
    if type(value) is not dict or set(value) != {"normalized", "sources"}:
        raise ValueError("facts snapshot must contain normalized and sources")
    normalized = value["normalized"]
    normalized_fields = {
        "binary_count", "archived", "last_commit", "committers_12m", "partial", "funding_yml",
    }
    if type(normalized) is not dict or set(normalized) != normalized_fields:
        raise ValueError("normalized snapshot fields do not match the contract")
    last_commit = normalized["last_commit"]
    if last_commit is not None:
        if type(last_commit) is not str:
            raise TypeError("normalized last_commit must be an ISO date string or None")
        last_commit = date.fromisoformat(last_commit)
    normalized_record = NormalizedFactsSnapshot(
        binary_count=normalized["binary_count"],
        archived=normalized["archived"],
        last_commit=last_commit,
        committers_12m=normalized["committers_12m"],
        partial=normalized["partial"],
        funding_yml=normalized["funding_yml"],
    )

    if type(value["sources"]) is not list:
        raise TypeError("snapshot sources must be a list")
    sources = []
    for raw in value["sources"]:
        source_fields = {
            "source_name", "repository_url", "homepage", "donation", "github", "partial", "resolution",
        }
        if type(raw) is not dict or set(raw) != source_fields:
            raise ValueError("source snapshot fields do not match the contract")
        github_raw = raw["github"]
        github = None
        if github_raw is not None:
            github_fields = {"full_name", "archived", "last_commit", "committers_12m", "funding_yml"}
            if type(github_raw) is not dict or set(github_raw) != github_fields:
                raise ValueError("GitHub snapshot fields do not match the contract")
            github = GitHubFactsSnapshot(**github_raw)
        resolution_raw = raw["resolution"]
        resolution_fields = {"method", "upstream_key", "evidence", "validated", "cached", "created_at"}
        if type(resolution_raw) is not dict or set(resolution_raw) != resolution_fields:
            raise ValueError("resolution snapshot fields do not match the contract")
        raw_evidence = resolution_raw["evidence"]
        evidence = None
        if raw_evidence is not None:
            if type(raw_evidence) is not list:
                raise TypeError("resolution evidence must be a list or None")
            if any(type(item) is not dict or set(item) != {"field", "value"} for item in raw_evidence):
                raise ValueError("evidence item fields do not match the contract")
            evidence = tuple(EvidenceItem(**item) for item in raw_evidence)
        timestamp = resolution_raw["created_at"]
        if type(timestamp) is not str:
            raise TypeError("resolution created_at must be an ISO timestamp")
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        sources.append(SourceFactsSnapshot(
            source_name=raw["source_name"],
            repository_url=raw["repository_url"],
            homepage=raw["homepage"],
            donation=raw["donation"],
            github=github,
            partial=raw["partial"],
            resolution=ResolutionSnapshot(
                method=resolution_raw["method"],
                upstream_key=resolution_raw["upstream_key"],
                evidence=evidence,
                validated=resolution_raw["validated"],
                cached=resolution_raw["cached"],
                created_at=timestamp,
            ),
        ))
    return FactsSnapshot(tuple(sources), normalized_record)

