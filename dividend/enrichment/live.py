"""Compose authoritative UDD metadata with GitHub structured repository facts."""

from .base import FactSet
from .github_client import GitHubClient
from .udd_client import UDDClient
from ..upstream.candidates import DISCOVERY_SOURCES, search_github_candidates
from ..upstream.source_evidence import collect_candidate_evidence
from ..upstream.verifier import ProjectSourceEvidence, verify_candidate, VerifiedRepository
from ..upstream.normalizer import normalize
import httpx


class LiveEnricher:
    def __init__(self, udd: UDDClient, github: GitHubClient, source_client=None):
        self.udd, self.github = udd, github
        self._owns_source_client = source_client is None
        self.source_client = source_client

    def fetch(self, source_names: list[str]) -> list[FactSet]:
        if self._owns_source_client:
            self.source_client = httpx.Client(timeout=5, follow_redirects=False)
        try:
            facts = self.udd.fetch(source_names)
        except Exception:
            facts = {name: FactSet(name, partial=True) for name in source_names}
        result = []
        try:
            for name in source_names:
                fact = facts[name]
                try:
                    enriched = self.github.enrich(fact)
                    if name in DISCOVERY_SOURCES:
                        enriched = self._discover_official_repository(enriched)
                    result.append(enriched)
                except Exception:
                    result.append(FactSet(name, fact.repository_url, fact.homepage, fact.donation,
                                          partial=True, contact=fact.contact))
            return result
        finally:
            if self._owns_source_client:
                self.source_client.close()
                self.source_client = None

    def _discover_official_repository(self, fact: FactSet) -> FactSet:
        candidates = search_github_candidates(self.github.client, fact.source_name, fact.homepage)
        if not candidates:
            return fact
        collection = collect_candidate_evidence(self.source_client, candidates[0])
        for candidate in candidates:
            link = next((item for item in collection.evidence
                         if item.normalized_repository_key == normalize(candidate.repository_url)), None)
            if link is None:
                continue
            verified = verify_candidate(candidate, ProjectSourceEvidence(
                source_url=link.source_url,
                linked_repository_url=link.observed_repository_url,
                verification_method="official_project_source_link",
                provenance=(f"{link.source_trust_policy_id}:{link.verification_method}:"
                            f"http-{link.http_status}:{link.retrieved_at.isoformat()}"),
            ))
            if not isinstance(verified, VerifiedRepository):
                continue
            github_facts = self.github.enrich(FactSet(fact.source_name, verified.canonical_repository_url))
            if (github_facts.github is None
                    or normalize(f"https://github.com/{github_facts.github.full_name}") != verified.upstream_key):
                continue
            return FactSet(
                fact.source_name, verified.canonical_repository_url, fact.homepage,
                fact.donation, github_facts.github, fact.partial, fact.contact,
                github_facts.github_owner_login, github_facts.github_parent_full_name,
                verified.canonical_repository_url, verified.official_project_source_url,
                verified.provenance,
            )
        return fact
