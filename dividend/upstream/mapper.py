"""Deterministic-first source package mapper."""

from dataclasses import dataclass
from typing import Literal

from ..enrichment.base import FactSet
from .normalizer import normalize
from .overrides import OverrideConfig, is_no_upstream, match_family


@dataclass(frozen=True, slots=True)
class Mapping:
    source_name: str
    upstream_key: str | None
    method: Literal["override", "repo_url", "none"]
    evidence: list[dict[str, str]]
    outcome: Literal["resolved", "unmapped", "no_upstream"]


def map_source(source: str, facts: FactSet, overrides: OverrideConfig) -> Mapping:
    if is_no_upstream(source, overrides):
        return Mapping(source, None, "none", [], "no_upstream")
    family = match_family(source, overrides)
    if family:
        return Mapping(source, family, "override", [], "resolved")
    key = normalize(facts.repository_url)
    if key:
        return Mapping(source, key, "repo_url", [{"field": "udd.repository", "value": facts.repository_url}], "resolved")
    return Mapping(source, None, "none", [], "unmapped")
