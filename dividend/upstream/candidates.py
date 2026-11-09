"""Read-only GitHub repository search records; candidates are never mappings."""

from dataclasses import dataclass
from urllib.parse import urlencode


DISCOVERY_SOURCES = frozenset({"curl", "systemd"})


@dataclass(frozen=True, slots=True)
class RepositoryCandidate:
    source_name: str
    repository_url: str
    owner: str
    name: str
    discovery_method: str
    evidence_sources: tuple[str, ...]
    github_description: str | None
    website: str | None
    archived: bool | None
    is_fork: bool | None
    evidence_classification: str = "search_candidate_unverified"
    default_branch: str | None = None


def search_github_candidates(client, source_name: str, homepage: str | None = None) -> tuple[RepositoryCandidate, ...]:
    """Search repositories by package name and return unverified records only."""
    if source_name not in DISCOVERY_SOURCES:
        raise ValueError("candidate search is limited to curl and systemd")
    query = f"{source_name} in:name"
    response = client.get("https://api.github.com/search/repositories",
                          params={"q": query, "per_page": 10})
    response.raise_for_status()
    payload = response.json()
    if type(payload) is not dict or type(payload.get("items")) is not list:
        raise ValueError("GitHub returned an invalid repository search response")
    evidence = (f"GitHub search source: https://api.github.com/search/repositories?{urlencode({'q': query, 'per_page': 10})}",)
    if homepage:
        evidence += (f"UDD homepage context: {homepage}",)
    candidates = []
    for item in payload["items"]:
        if type(item) is not dict:
            continue
        full_name = item.get("full_name")
        parts = full_name.split("/") if type(full_name) is str else []
        owner = item.get("owner")
        if (len(parts) != 2 or not all(parts) or type(owner) is not dict
                or owner.get("login") != parts[0] or item.get("name") != parts[1]):
            continue
        candidates.append(RepositoryCandidate(
            source_name=source_name,
            repository_url=f"https://github.com/{parts[0]}/{parts[1]}",
            owner=parts[0], name=parts[1],
            discovery_method="github_repository_search", evidence_sources=evidence,
            github_description=item.get("description") if type(item.get("description")) is str else None,
            website=item.get("homepage") if type(item.get("homepage")) is str and item["homepage"] else None,
            archived=item.get("archived") if type(item.get("archived")) is bool else None,
            is_fork=item.get("fork") if type(item.get("fork")) is bool else None,
            default_branch=item.get("default_branch") if type(item.get("default_branch")) is str else None,
        ))
    return tuple(candidates)
