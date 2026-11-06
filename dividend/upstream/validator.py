"""Validate structured resolver proposals against exact source facts."""

from dataclasses import dataclass

from ..enrichment.base import FactSet
from .normalizer import normalize

EVIDENCE_ALLOWLIST = {"udd.repository", "udd.homepage", "udd.contact", "github.full_name", "github.owner_login", "github.parent_full_name"}
_URL_FIELDS = {"udd.repository", "udd.homepage"}


@dataclass(frozen=True, slots=True)
class Proposal:
    source_name: str
    upstream_key: str
    display_name: str
    evidence: list[dict[str, str]]


def fact_values(facts: FactSet) -> dict[str, str | None]:
    return {
        "udd.repository": facts.repository_url,
        "udd.homepage": facts.homepage,
        "udd.contact": facts.contact,
        "github.full_name": facts.github.full_name if facts.github else None,
        "github.owner_login": facts.github_owner_login,
        "github.parent_full_name": facts.github_parent_full_name,
    }


def validate(proposal: Proposal, facts: FactSet, existing_keys: set[str] | None = None) -> bool:
    if (type(proposal) is not Proposal or proposal.source_name != facts.source_name
            or type(proposal.upstream_key) is not str or not proposal.upstream_key.strip()
            or proposal.upstream_key != proposal.upstream_key.strip()
            or type(proposal.display_name) is not str or not proposal.display_name.strip()
            or type(proposal.evidence) is not list or not proposal.evidence):
        return False
    available = fact_values(facts)
    cited_urls: set[str] = set()
    cited_fields: set[str] = set()
    for item in proposal.evidence:
        if type(item) is not dict or set(item) != {"field", "value"}:
            return False
        field, value = item["field"], item["value"]
        if type(field) is not str or field not in EVIDENCE_ALLOWLIST or type(value) is not str or not value.strip() or available.get(field) != value or field in cited_fields:
            return False
        cited_fields.add(field)
        if field in _URL_FIELDS:
            key = normalize(value)
            if key:
                cited_urls.add(key)
    if len(cited_urls) > 1:
        return False
    return proposal.upstream_key in cited_urls or proposal.upstream_key in (existing_keys or set())
