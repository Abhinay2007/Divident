"""Bounded read-only collection of repository links from reviewed project pages."""

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from .candidates import RepositoryCandidate
from .normalizer import normalize
from .verifier import TRUSTED_PROJECT_DOMAINS, TRUSTED_PROJECT_SOURCES, TRUST_POLICY_ID

_MAX_REDIRECTS = 3
_MAX_PAGE_BYTES = 1_000_000
_REPOSITORY_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.savannah.gnu.org"}


@dataclass(frozen=True, slots=True)
class RepositoryLinkEvidence:
    source_name: str
    source_url: str
    observed_href: str
    observed_repository_url: str
    normalized_repository_key: str
    retrieved_at: datetime
    http_status: int
    verification_method: Literal["reviewed_project_page_html_link"]
    source_trust_policy_id: str


@dataclass(frozen=True, slots=True)
class SourceCollectionResult:
    source_name: str
    source_url: str
    final_source_url: str | None
    retrieved_at: datetime
    http_status: int | None
    page_loaded: bool
    candidate_key: str | None
    evidence: tuple[RepositoryLinkEvidence, ...]
    status: Literal["candidate_link_found", "page_loaded_unverified", "http_error",
                    "untrusted_source", "redirect_limit", "timeout", "fetch_error", "page_too_large"]

    @property
    def candidate_link_found(self) -> bool:
        return any(item.normalized_repository_key == self.candidate_key for item in self.evidence)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.hrefs.extend(value for key, value in attrs if key.lower() == "href" and value is not None)


def _safe_https_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                and parsed.username is None and parsed.password is None and parsed.port is None)
    except (TypeError, ValueError):
        return False


def collect_candidate_evidence(client, candidate: RepositoryCandidate) -> SourceCollectionResult:
    """Fetch only the configured project page and report explicit HTML repository links."""
    now = datetime.now(timezone.utc)
    source_name = getattr(candidate, "source_name", None)
    if (type(candidate) is not RepositoryCandidate or type(source_name) is not str
            or source_name not in TRUSTED_PROJECT_SOURCES
            or candidate.evidence_classification != "search_candidate_unverified"):
        return SourceCollectionResult(str(source_name), "", None, now, None, False, None, (), "untrusted_source")
    source_url = TRUSTED_PROJECT_SOURCES[source_name]
    candidate_key = normalize(candidate.repository_url)
    if not candidate_key:
        return SourceCollectionResult(source_name, source_url, None, now, None, False, None, (), "page_loaded_unverified")
    allowed_hosts = TRUSTED_PROJECT_DOMAINS[source_name]
    current_url = source_url
    response = None
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            if not _safe_https_url(current_url, allowed_hosts):
                return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc), None,
                                              False, candidate_key, (), "untrusted_source")
            with client.stream("GET", current_url, headers={"Accept": "text/html"}, timeout=5.0,
                               follow_redirects=False) as response:
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    if redirect_count == _MAX_REDIRECTS:
                        return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc),
                                                      status, False, candidate_key, (), "redirect_limit")
                    location = response.headers.get("location")
                    if type(location) is not str or not location.strip():
                        return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc),
                                                      status, False, candidate_key, (), "http_error")
                    try:
                        current_url = urljoin(current_url, location)
                    except (TypeError, ValueError):
                        return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc),
                                                      status, False, candidate_key, (), "untrusted_source")
                    continue
                retrieved_at = datetime.now(timezone.utc)
                if not 200 <= status < 300:
                    return SourceCollectionResult(source_name, source_url, current_url, retrieved_at,
                                                  status, False, candidate_key, (), "http_error")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_PAGE_BYTES:
                        return SourceCollectionResult(source_name, source_url, current_url, retrieved_at,
                                                      status, False, candidate_key, (), "page_too_large")
                response_encoding = response.encoding or "utf-8"
                break
    except (httpx.TimeoutException, TimeoutError):
        return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc), None,
                                      False, candidate_key, (), "timeout")
    except (httpx.RequestError, OSError, ValueError):
        return SourceCollectionResult(source_name, source_url, current_url, datetime.now(timezone.utc), None,
                                      False, candidate_key, (), "fetch_error")

    parser = _Links()
    try:
        parser.feed(bytes(body).decode(response_encoding, errors="replace"))
    except (LookupError, ValueError):
        return SourceCollectionResult(source_name, source_url, current_url, retrieved_at,
                                      response.status_code, False, candidate_key, (), "http_error")

    links = []
    for href in parser.hrefs:
        try:
            observed_url = urljoin(current_url, href)
        except (TypeError, ValueError):
            continue
        key = normalize(observed_url)
        try:
            parsed = urlsplit(observed_url)
            host = parsed.hostname
            safe = (parsed.scheme == "https" and parsed.username is None
                    and parsed.password is None and parsed.port is None)
        except (TypeError, ValueError):
            continue
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if host in {"github.com", "bitbucket.org"} and len(path.split("/")) != 2:
            continue
        if key and safe and (host in _REPOSITORY_HOSTS or observed_url.lower().endswith(".git")):
            links.append(RepositoryLinkEvidence(
                source_name=source_name, source_url=source_url, observed_href=href,
                observed_repository_url=observed_url, normalized_repository_key=key,
                retrieved_at=retrieved_at, http_status=response.status_code,
                verification_method="reviewed_project_page_html_link",
                source_trust_policy_id=TRUST_POLICY_ID,
            ))
    deduped = tuple({(item.observed_href, item.normalized_repository_key): item for item in links}.values())
    found = any(item.normalized_repository_key == candidate_key for item in deduped)
    return SourceCollectionResult(
        source_name=source_name, source_url=source_url, final_source_url=current_url,
        retrieved_at=retrieved_at, http_status=response.status_code, page_loaded=True,
        candidate_key=candidate_key, evidence=deduped,
        status="candidate_link_found" if found else "page_loaded_unverified",
    )
