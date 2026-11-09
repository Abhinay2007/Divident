"""Pure deterministic verification of candidate repositories."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from .candidates import RepositoryCandidate
from .normalizer import normalize


# Reviewed official project pages for the three-package discovery pilot.
TRUST_POLICY_ID = "official-project-pages-v1"
TRUSTED_PROJECT_SOURCES = {
    "curl": "https://curl.se/dev/source.html",
    "systemd": "https://systemd.io/BACKPORTS/",
}
TRUSTED_PROJECT_DOMAINS = {name: frozenset({urlsplit(url).hostname})
                           for name, url in TRUSTED_PROJECT_SOURCES.items()}


@dataclass(frozen=True, slots=True)
class ProjectSourceEvidence:
    source_url: str
    linked_repository_url: str
    verification_method: Literal["official_project_source_link"]
    provenance: str


@dataclass(frozen=True, slots=True)
class VerifiedRepository:
    source_name: str
    upstream_key: str
    canonical_repository_url: str
    hosting_provider: str
    official_project_source_url: str
    observed_repository_url: str
    verification_method: str
    provenance: str


@dataclass(frozen=True, slots=True)
class VerificationRejection:
    reason: Literal[
        "invalid_candidate", "untrusted_source", "invalid_evidence",
        "invalid_repository_url", "repository_mismatch", "debian_packaging_repository",
    ]


VerificationResult = VerifiedRepository | VerificationRejection


def _trusted_source(source_name: str, source_url: str) -> bool:
    try:
        parsed = urlsplit(source_url)
        trusted_url = TRUSTED_PROJECT_SOURCES.get(source_name)
        return (trusted_url is not None and parsed.scheme == "https"
                and parsed.hostname in TRUSTED_PROJECT_DOMAINS.get(source_name, ())
                and parsed.username is None and parsed.password is None and parsed.port is None
                and not parsed.query and not parsed.fragment
                and source_url.rstrip("/") == trusted_url.rstrip("/"))
    except (TypeError, ValueError):
        return False


def verify_candidate(candidate: RepositoryCandidate, evidence: ProjectSourceEvidence) -> VerificationResult:
    """Verify a candidate only when an allowlisted official source explicitly links to it."""
    if (type(candidate) is not RepositoryCandidate or type(candidate.source_name) is not str
            or candidate.evidence_classification != "search_candidate_unverified"
            or candidate.source_name not in TRUSTED_PROJECT_DOMAINS):
        return VerificationRejection("invalid_candidate")
    candidate_key = normalize(candidate.repository_url)
    if not candidate_key:
        return VerificationRejection("invalid_repository_url")
    try:
        candidate_url = urlsplit(candidate.repository_url)
        if candidate_url.hostname == "debian.org" or (candidate_url.hostname or "").endswith(".debian.org"):
            return VerificationRejection("debian_packaging_repository")
        if (candidate_url.scheme != "https" or candidate_url.hostname != "github.com"
                or candidate_url.username is not None or candidate_url.password is not None
                or candidate_url.port is not None):
            return VerificationRejection("invalid_candidate")
    except (TypeError, ValueError):
        return VerificationRejection("invalid_candidate")
    if type(evidence) is not ProjectSourceEvidence:
        return VerificationRejection("invalid_evidence")
    if type(evidence.source_url) is not str:
        return VerificationRejection("invalid_evidence")
    if not _trusted_source(candidate.source_name, evidence.source_url):
        return VerificationRejection("untrusted_source")
    if (evidence.verification_method != "official_project_source_link"
            or type(evidence.provenance) is not str or not evidence.provenance.strip()
            or evidence.provenance != evidence.provenance.strip()
            or type(evidence.linked_repository_url) is not str
            or not evidence.linked_repository_url.strip()):
        return VerificationRejection("invalid_evidence")
    try:
        linked_url = urlsplit(evidence.linked_repository_url)
        if (linked_url.scheme != "https" or linked_url.username is not None
                or linked_url.password is not None or linked_url.port is not None):
            return VerificationRejection("invalid_repository_url")
    except (TypeError, ValueError):
        return VerificationRejection("invalid_repository_url")
    observed_key = normalize(evidence.linked_repository_url)
    if not candidate_key or not observed_key:
        return VerificationRejection("invalid_repository_url")
    if observed_key.split("/", 1)[0].endswith(".debian.org") or observed_key.split("/", 1)[0] == "debian.org":
        return VerificationRejection("debian_packaging_repository")
    if candidate_key != observed_key:
        return VerificationRejection("repository_mismatch")
    return VerifiedRepository(
        source_name=candidate.source_name,
        upstream_key=candidate_key,
        canonical_repository_url=f"https://{candidate_key}",
        hosting_provider=candidate_key.split("/", 1)[0],
        official_project_source_url=evidence.source_url,
        observed_repository_url=evidence.linked_repository_url,
        verification_method=evidence.verification_method,
        provenance=evidence.provenance,
    )
