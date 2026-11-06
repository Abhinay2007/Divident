"""Small, structured enrichment contract; never carries document bodies."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GitHubFacts:
    full_name: str
    archived: bool = False
    last_commit: str | None = None
    committers_12m: int | None = None
    funding_yml: bool = False


@dataclass(frozen=True, slots=True)
class FactSet:
    source_name: str
    repository_url: str | None = None
    homepage: str | None = None
    donation: str | None = None
    github: GitHubFacts | None = None
    partial: bool = False
    contact: str | None = None
    github_owner_login: str | None = None
    github_parent_full_name: str | None = None


class Enricher(Protocol):
    def fetch(self, source_names: list[str]) -> list[FactSet]: ...
