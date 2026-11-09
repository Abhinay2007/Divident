from sqlalchemy import func, select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.enrichment.base import FactSet
from dividend.models import UpstreamProject
from dividend.upstream.candidates import RepositoryCandidate, search_github_candidates
from dividend.upstream.mapper import map_source
from dividend.upstream.overrides import OverrideConfig
from dividend.upstream.validator import Proposal


def test_candidate_search_returns_unverified_records_without_mapping_or_persistence(tmp_path):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"items": [{
                "full_name": "curl/curl", "name": "curl",
                "owner": {"login": "curl"}, "description": "curl source",
                "homepage": "https://curl.se", "archived": False, "fork": False,
            }]}

    class Client:
        def get(self, url, *, params):
            assert url == "https://api.github.com/search/repositories"
            assert params == {"q": "curl in:name", "per_page": 10}
            return Response()

    candidates = search_github_candidates(Client(), "curl", "https://curl.se")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, RepositoryCandidate)
    assert not isinstance(candidate, Proposal)
    assert candidate.evidence_classification == "search_candidate_unverified"
    assert candidate.repository_url == "https://github.com/curl/curl"
    assert candidate.website == "https://curl.se" and candidate.archived is False

    # Candidate data is not input to the mapper and cannot establish identity.
    facts = FactSet("curl", homepage="https://curl.se")
    assert map_source("curl", facts, OverrideConfig()).outcome == "unmapped"

    engine = create_database_engine(f"sqlite:///{tmp_path / 'candidate-only.db'}")
    initialize_database(engine)
    try:
        with create_session_factory(engine)() as db:
            assert db.scalar(select(func.count()).select_from(UpstreamProject)) == 0
    finally:
        engine.dispose()


def test_candidate_search_is_bounded_to_the_evaluation_packages():
    class UnusedClient:
        def get(self, *_args, **_kwargs): raise AssertionError("out-of-scope search must not run")

    try:
        search_github_candidates(UnusedClient(), "openssl")
    except ValueError as exc:
        assert "limited to curl and systemd" in str(exc)
    else:
        raise AssertionError("out-of-scope package was searched")
