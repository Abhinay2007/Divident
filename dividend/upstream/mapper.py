"""Deterministic-first source package mapper."""

from dataclasses import dataclass
from typing import Literal

from ..enrichment.base import FactSet
from .normalizer import normalize
from .overrides import OverrideConfig, is_no_upstream, match_family
from .candidates import RepositoryCandidate
from .verifier import ProjectSourceEvidence, VerifiedRepository, verify_candidate


@dataclass(frozen=True, slots=True)
class Mapping:
    source_name: str
    upstream_key: str | None
    method: Literal["override", "repo_url", "official_source", "none"]
    evidence: list[dict[str, str]]
    outcome: Literal["resolved", "unmapped", "no_upstream"]
    validated: bool = False


def map_source(source: str, facts: FactSet, overrides: OverrideConfig) -> Mapping:
    if is_no_upstream(source, overrides):
        return Mapping(source, None, "none", [], "no_upstream")
    family = match_family(source, overrides)
    if family:
        return Mapping(source, family, "override", [
            {"field": "override.source", "value": source},
            {"field": "override.upstream_key", "value": family},
            {"field": "override.provenance", "value": "data/upstream_overrides.yaml"},
        ], "resolved", True)
    official_claim = any((facts.official_repository_url, facts.official_project_source_url,
                          facts.official_provenance))
    if facts.official_repository_url and facts.official_project_source_url and facts.official_provenance:
        key = normalize(facts.official_repository_url)
        parts = key.split("/", 1) if key else []
        if len(parts) == 2 and parts[0] == "github.com":
            candidate = RepositoryCandidate(
                source, facts.official_repository_url, parts[1].split("/", 1)[0],
                parts[1].split("/", 1)[1], "github_repository_search", (), None, None,
                False, False,
            )
            verified = verify_candidate(candidate, ProjectSourceEvidence(
                facts.official_project_source_url, facts.official_repository_url,
                "official_project_source_link", facts.official_provenance,
            ))
            identity = (normalize(f"https://{facts.github.full_name}") if facts.github and facts.github.full_name.startswith("github.com/")
                        else normalize(f"https://github.com/{facts.github.full_name}") if facts.github else None)
            if isinstance(verified, VerifiedRepository) and identity == verified.upstream_key:
                return Mapping(source, verified.upstream_key, "official_source", [
                    {"field": "official.project_source", "value": verified.official_project_source_url},
                    {"field": "official.repository_link", "value": verified.observed_repository_url},
                    {"field": "official.provenance", "value": verified.provenance},
                    {"field": "github.full_name", "value": facts.github.full_name},
                ], "resolved", True)
    if official_claim:
        return Mapping(source, None, "none", [], "unmapped")
    key = normalize(facts.repository_url)
    if key:
        return Mapping(source, key, "repo_url", [{"field": "udd.repository", "value": facts.repository_url}], "resolved")
    return Mapping(source, None, "none", [], "unmapped")
