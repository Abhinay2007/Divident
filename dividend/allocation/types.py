"""Immutable inputs for the pure allocation domain."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime


def _require_bool_or_none(field: str, value: object) -> None:
    if value is not None and type(value) is not bool:
        raise TypeError(f"{field} must be a bool or None")


def _require_nonnegative_int_or_none(field: str, value: object) -> None:
    if value is None:
        return
    if type(value) is not int:
        raise TypeError(f"{field} must be an int or None")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProjectFacts:
    """Normalized project signals and their audit-only source snapshot."""

    archived: bool | None
    last_commit: date | None
    committers_12m: int | None
    partial: bool
    funding_yml: bool | None
    snapshot: "FactsSnapshot"

    def __post_init__(self) -> None:
        _require_bool_or_none("archived", self.archived)
        if self.last_commit is not None and type(self.last_commit) is not date:
            raise TypeError("last_commit must be a date or None")
        _require_nonnegative_int_or_none("committers_12m", self.committers_12m)
        if type(self.partial) is not bool:
            raise TypeError("partial must be a bool")
        _require_bool_or_none("funding_yml", self.funding_yml)
        if not isinstance(self.snapshot, FactsSnapshot):
            raise TypeError("snapshot must be FactsSnapshot")
        normalized = self.snapshot.normalized
        actual = (self.archived, self.last_commit, self.committers_12m, self.partial, self.funding_yml)
        recorded = (normalized.archived, normalized.last_commit, normalized.committers_12m, normalized.partial, normalized.funding_yml)
        if actual != recorded:
            raise ValueError("snapshot normalized facts must match ProjectFacts")


def _require_trimmed_text(field: str, value: object, max_length: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field} must be trimmed")
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")


@dataclass(frozen=True, slots=True)
class ProjectInput:
    """A resolved eligible project and caller-derived payability input."""

    upstream_key: str
    display_name: str
    binary_count: int
    facts: ProjectFacts
    opted_in: bool

    def __post_init__(self) -> None:
        _require_trimmed_text("upstream_key", self.upstream_key, 256)
        _require_trimmed_text("display_name", self.display_name, 200)
        if type(self.binary_count) is not int:
            raise TypeError("binary_count must be an int")
        if self.binary_count <= 0:
            raise ValueError("binary_count must be positive")
        if not isinstance(self.facts, ProjectFacts):
            raise TypeError("facts must be ProjectFacts")
        if type(self.opted_in) is not bool:
            raise TypeError("opted_in must be a bool")
        if self.facts.snapshot.normalized.binary_count != self.binary_count:
            raise ValueError("snapshot binary_count must match ProjectInput.binary_count")


def _strict_str(field: str, value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str:
        raise TypeError(f"{field} must be a string" + (" or None" if nullable else ""))


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    field: str
    value: str

    def __post_init__(self) -> None:
        _strict_str("field", self.field)
        _strict_str("value", self.value)


@dataclass(frozen=True, slots=True)
class ResolutionSnapshot:
    method: str
    upstream_key: str | None
    evidence: tuple[EvidenceItem, ...] | None
    validated: bool
    cached: bool
    created_at: datetime

    def __post_init__(self) -> None:
        if self.method not in {"override", "repo_url", "llm", "none"}:
            raise ValueError("method must be override, repo_url, llm, or none")
        _strict_str("upstream_key", self.upstream_key, nullable=True)
        if self.evidence is not None and (type(self.evidence) is not tuple or not all(type(item) is EvidenceItem for item in self.evidence)):
            raise TypeError("evidence must be a tuple of EvidenceItem or None")
        if type(self.validated) is not bool or type(self.cached) is not bool:
            raise TypeError("validated and cached must be bools")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise TypeError("created_at must be a timezone-aware datetime")
        if self.created_at.utcoffset().total_seconds() != 0:
            raise ValueError("created_at must be UTC")
        if self.evidence is not None:
            object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda item: (item.field, item.value))))


@dataclass(frozen=True, slots=True)
class GitHubFactsSnapshot:
    full_name: str
    archived: bool
    last_commit: str | None
    committers_12m: int | None
    funding_yml: bool

    def __post_init__(self) -> None:
        _strict_str("full_name", self.full_name)
        _strict_str("last_commit", self.last_commit, nullable=True)
        if type(self.archived) is not bool or type(self.funding_yml) is not bool:
            raise TypeError("archived and funding_yml must be bools")
        _require_nonnegative_int_or_none("committers_12m", self.committers_12m)


@dataclass(frozen=True, slots=True)
class SourceFactsSnapshot:
    source_name: str
    repository_url: str | None
    homepage: str | None
    donation: str | None
    github: GitHubFactsSnapshot | None
    partial: bool
    resolution: ResolutionSnapshot

    def __post_init__(self) -> None:
        _strict_str("source_name", self.source_name)
        for field in ("repository_url", "homepage", "donation"):
            _strict_str(field, getattr(self, field), nullable=True)
        if self.github is not None and type(self.github) is not GitHubFactsSnapshot:
            raise TypeError("github must be GitHubFactsSnapshot or None")
        if type(self.partial) is not bool:
            raise TypeError("partial must be a bool")
        if type(self.resolution) is not ResolutionSnapshot:
            raise TypeError("resolution must be ResolutionSnapshot")


@dataclass(frozen=True, slots=True)
class NormalizedFactsSnapshot:
    binary_count: int
    archived: bool | None
    last_commit: date | None
    committers_12m: int | None
    partial: bool
    funding_yml: bool | None

    def __post_init__(self) -> None:
        if type(self.binary_count) is not int or self.binary_count <= 0:
            raise ValueError("binary_count must be a positive integer")
        _require_bool_or_none("archived", self.archived)
        if self.last_commit is not None and type(self.last_commit) is not date:
            raise TypeError("last_commit must be a date or None")
        _require_nonnegative_int_or_none("committers_12m", self.committers_12m)
        if type(self.partial) is not bool:
            raise TypeError("partial must be a bool")
        _require_bool_or_none("funding_yml", self.funding_yml)


@dataclass(frozen=True, slots=True)
class FactsSnapshot:
    sources: tuple[SourceFactsSnapshot, ...]
    normalized: NormalizedFactsSnapshot

    def __post_init__(self) -> None:
        if type(self.sources) is not tuple or not all(type(source) is SourceFactsSnapshot for source in self.sources):
            raise TypeError("sources must be a tuple of SourceFactsSnapshot")
        names = [source.source_name for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=lambda source: source.source_name)))
        if type(self.normalized) is not NormalizedFactsSnapshot:
            raise TypeError("normalized must be NormalizedFactsSnapshot")
        if self.normalized.partial != any(source.partial for source in self.sources):
            raise ValueError("normalized partial must equal any source partial")

    def to_dict(self) -> dict[str, object]:
        """Return explicit JSON-compatible data; arbitrary objects are rejected."""
        return {
            "normalized": {
                "archived": self.normalized.archived,
                "binary_count": self.normalized.binary_count,
                "committers_12m": self.normalized.committers_12m,
                "funding_yml": self.normalized.funding_yml,
                "last_commit": self.normalized.last_commit.isoformat() if self.normalized.last_commit else None,
                "partial": self.normalized.partial,
            },
            "sources": [
                {
                    "donation": source.donation,
                    "github": None if source.github is None else {
                        "archived": source.github.archived,
                        "committers_12m": source.github.committers_12m,
                        "full_name": source.github.full_name,
                        "funding_yml": source.github.funding_yml,
                        "last_commit": source.github.last_commit,
                    },
                    "homepage": source.homepage,
                    "partial": source.partial,
                    "repository_url": source.repository_url,
                    "resolution": {
                        "cached": source.resolution.cached,
                        "created_at": source.resolution.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                        "evidence": None if source.resolution.evidence is None else [{"field": item.field, "value": item.value} for item in source.resolution.evidence],
                        "method": source.resolution.method,
                        "upstream_key": source.resolution.upstream_key,
                        "validated": source.resolution.validated,
                    },
                    "source_name": source.source_name,
                }
                for source in self.sources
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
