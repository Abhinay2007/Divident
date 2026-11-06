"""JSON fixture enricher with strict structured-field decoding."""

import json
from pathlib import Path

from .base import FactSet, GitHubFacts


class FixtureEnricher:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def fetch(self, source_names: list[str]) -> list[FactSet]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        records = raw.get("sources", raw) if type(raw) is dict else None
        if type(records) is not dict:
            raise ValueError("enrichment fixture must contain a source object")
        result = []
        for name in source_names:
            entry = records.get(name, {})
            if type(entry) is not dict:
                raise ValueError(f"invalid facts for {name}")
            gh = entry.get("github")
            github = GitHubFacts(**gh) if type(gh) is dict else None
            result.append(FactSet(source_name=name, repository_url=entry.get("repository_url", entry.get("Repository")),
                                  homepage=entry.get("homepage", entry.get("Homepage")), donation=entry.get("donation"),
                                  github=github, partial=entry.get("partial", True), contact=entry.get("contact", entry.get("Contact")),
                                  github_owner_login=entry.get("owner_login"), github_parent_full_name=entry.get("parent_full_name")))
        return result
