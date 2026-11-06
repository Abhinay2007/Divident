"""Compose authoritative UDD metadata with GitHub structured repository facts."""

from .base import FactSet
from .github_client import GitHubClient
from .udd_client import UDDClient


class LiveEnricher:
    def __init__(self, udd: UDDClient, github: GitHubClient):
        self.udd, self.github = udd, github

    def fetch(self, source_names: list[str]) -> list[FactSet]:
        try:
            facts = self.udd.fetch(source_names)
        except Exception:
            facts = {name: FactSet(name, partial=True) for name in source_names}
        result = []
        for name in source_names:
            fact = facts[name]
            try:
                result.append(self.github.enrich(fact))
            except Exception:
                result.append(FactSet(name, fact.repository_url, fact.homepage, fact.donation,
                                      partial=True, contact=fact.contact))
        return result
