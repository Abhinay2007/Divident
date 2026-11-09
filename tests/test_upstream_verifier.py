import json
from pathlib import Path

from sqlalchemy import func, select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.enrichment.base import FactSet
from dividend.models import UpstreamProject
from dividend.upstream.candidates import RepositoryCandidate
from dividend.upstream.mapper import map_source
from dividend.upstream.normalizer import normalize
from dividend.upstream.overrides import OverrideConfig
from dividend.upstream.verifier import ProjectSourceEvidence, VerifiedRepository, verify_candidate


FIXTURE = json.loads((Path(__file__).parent / "fixtures/upstream_verification_evidence.json").read_text())


def candidate(source_name, repository_url, *, default_branch=None):
    key = normalize(repository_url)
    path = key.split("/", 1)[1].split("/") if key else ["invalid", source_name]
    return RepositoryCandidate(
        source_name=source_name, repository_url=repository_url, owner=path[-2], name=path[-1],
        discovery_method="reviewed_test_fixture", evidence_sources=(FIXTURE["label"],),
        github_description=None, website=None, archived=None, is_fork=None,
        default_branch=default_branch,
    )


def evidence(source_url, linked_repository_url, *, method="official_project_source_link", provenance="reviewed test evidence"):
    return ProjectSourceEvidence(source_url, linked_repository_url, method, provenance)


def test_reviewed_curl_and_systemd_fixtures_verify_without_live_claims():
    assert "reviewed test evidence only; not fetched or live-verified" in FIXTURE["label"]
    for case in FIXTURE["cases"]:
        if case["source_name"] == "bash":
            continue
        result = verify_candidate(
            candidate(case["source_name"], case["repository_url"]),
            evidence(case["project_source_url"], case["linked_repository_url"], provenance=FIXTURE["label"]),
        )
        assert isinstance(result, VerifiedRepository)
        assert result.source_name == case["source_name"]
        assert result.upstream_key == normalize(case["repository_url"])
        assert result.canonical_repository_url == "https://" + normalize(case["repository_url"])
        assert result.hosting_provider == normalize(case["repository_url"]).split("/", 1)[0]
        assert result.observed_repository_url == case["linked_repository_url"]
        assert result.provenance == FIXTURE["label"]


def test_homepage_only_and_github_search_only_are_rejected():
    item = candidate("curl", "https://github.com/curl/curl")
    assert verify_candidate(item, None).reason == "invalid_evidence"
    assert verify_candidate(item, evidence("https://curl.se/dev/source.html", "https://github.com/curl/curl",
                                           method="homepage_only")).reason == "invalid_evidence"


def test_wrong_repository_and_untrusted_source_are_rejected():
    item = candidate("curl", "https://github.com/curl/curl")
    assert verify_candidate(item, evidence("https://curl.se/dev/source.html", "https://github.com/other/curl")).reason == "repository_mismatch"
    assert verify_candidate(item, evidence("https://example.org/", item.repository_url)).reason == "untrusted_source"
    assert verify_candidate(item, evidence("https://curl.se/dev/source.html", "http://github.com/curl/curl")).reason == "invalid_repository_url"
    assert verify_candidate(candidate("curl", "http://github.com/curl/curl"), evidence("https://curl.se/dev/source.html", item.repository_url)).reason == "invalid_candidate"


def test_debian_packaging_repository_and_invalid_url_are_rejected():
    packaging = candidate("curl", "https://salsa.debian.org/debian/curl")
    assert verify_candidate(packaging, evidence("https://curl.se/dev/source.html", packaging.repository_url)).reason == "debian_packaging_repository"
    invalid = candidate("curl", "not a repository URL")
    assert verify_candidate(invalid, evidence("https://curl.se/dev/source.html", invalid.repository_url)).reason == "invalid_repository_url"


def test_missing_or_malformed_provenance_is_rejected():
    item = candidate("curl", "https://github.com/curl/curl")
    assert verify_candidate(item, evidence("https://curl.se/dev/source.html", item.repository_url,
                                           provenance="")).reason == "invalid_evidence"
    assert verify_candidate(item, evidence("https://curl.se/dev/source.html", item.repository_url,
                                           provenance=" reviewed ")).reason == "invalid_evidence"


def test_bash_is_not_accepted_until_its_official_source_is_reviewed():
    item = candidate("bash", "https://git.savannah.gnu.org/cgit/bash.git")
    assert verify_candidate(item, evidence("https://www.gnu.org/software/bash/", item.repository_url)).reason == "invalid_candidate"


def test_default_branch_is_optional_and_does_not_change_identity():
    repo = "https://github.com/systemd/systemd"
    proof = evidence("https://systemd.io/BACKPORTS/", repo)
    absent = verify_candidate(candidate("systemd", repo), proof)
    present_candidate = candidate("systemd", repo, default_branch="main")
    present = verify_candidate(present_candidate, proof)
    assert present_candidate.default_branch == "main"
    assert absent == present


def test_verified_result_does_not_enter_mapping_or_persistence(tmp_path):
    case = FIXTURE["cases"][1]
    result = verify_candidate(candidate(case["source_name"], case["repository_url"]),
                              evidence(case["project_source_url"], case["linked_repository_url"]))
    assert isinstance(result, VerifiedRepository)
    assert not isinstance(result, RepositoryCandidate)
    assert map_source("curl", FactSet("curl", homepage="https://curl.se"), OverrideConfig()).outcome == "unmapped"

    engine = create_database_engine(f"sqlite:///{tmp_path / 'verification.db'}")
    initialize_database(engine)
    try:
        with create_session_factory(engine)() as db:
            assert db.scalar(select(func.count()).select_from(UpstreamProject)) == 0
    finally:
        engine.dispose()
