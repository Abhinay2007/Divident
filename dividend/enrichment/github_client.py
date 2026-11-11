"""Fetch structured GitHub repository signals without reading repository files."""

from urllib.parse import urlsplit

import httpx

from .base import FactSet, GitHubFacts


class GitHubClient:
    def __init__(self, token: str, client: httpx.Client | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(timeout=10, headers=headers)

    def enrich(self, fact: FactSet) -> FactSet:
        url = fact.repository_url
        if not url:
            return fact
        parsed = urlsplit(url.removeprefix("git+"))
        if parsed.hostname not in {"github.com", "www.github.com"}:
            return fact
        parts = parsed.path.strip("/").removesuffix(".git").split("/")
        if len(parts) < 2:
            return fact
        response = self.client.get(f"https://api.github.com/repos/{parts[0]}/{parts[1]}")
        response.raise_for_status()
        repo = response.json()
        if type(repo) is not dict or type(repo.get("full_name")) is not str:
            raise ValueError("GitHub returned an invalid repository response")
        base = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
        commits_response = self.client.get(f"{base}/commits", params={"per_page": 1})
        commits_response.raise_for_status()
        commits = commits_response.json()
        last_commit = None
        if type(commits) is list and commits and type(commits[0]) is dict:
            commit = commits[0].get("commit")
            author = commit.get("author") if type(commit) is dict else None
            if type(author) is dict and type(author.get("date")) is str:
                last_commit = author["date"]
        funding_yml = False
        for path in ("FUNDING.yml", ".github/FUNDING.yml"):
            funding_response = self.client.get(f"{base}/contents/{path}")
            if funding_response.status_code == 200:
                funding_yml = True
                break
            if funding_response.status_code != 404:
                funding_response.raise_for_status()
        parent = repo.get("parent")
        owner = repo.get("owner")
        facts = GitHubFacts(
            full_name=repo["full_name"], archived=repo.get("archived") is True,
            last_commit=last_commit, committers_12m=None, funding_yml=funding_yml,
        )
        return FactSet(fact.source_name, fact.repository_url, fact.homepage, fact.donation, facts,
                       fact.partial, fact.contact,
                       owner.get("login") if type(owner) is dict and type(owner.get("login")) is str else None,
                       parent.get("full_name") if type(parent) is dict and type(parent.get("full_name")) is str else None)
