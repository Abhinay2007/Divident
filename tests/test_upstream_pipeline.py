from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.enrichment.base import FactSet, GitHubFacts
from dividend.enrichment.live import LiveEnricher
from dividend.models import SourceResolution, UpstreamProject
from dividend.services import SessionService
from dividend.upstream.mapper import map_source
from dividend.upstream.overrides import OverrideConfig
from dividend.upstream.verifier import TRUSTED_PROJECT_SOURCES


class Response:
    status_code = 200
    headers = {}
    encoding = "utf-8"

    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def iter_bytes(self): yield self.body.encode()


class SourceClient:
    def __init__(self, repo): self.repo = repo
    def stream(self, _method, _url, **_kwargs):
        return Response(f'<a href="{self.repo}">Upstream repository</a>')


class SearchResponse:
    def __init__(self, source, owner, repo):
        self.payload = {"items": [{"full_name": f"{owner}/{repo}", "owner": {"login": owner},
                                   "name": repo, "fork": False, "archived": False}]}
    def raise_for_status(self): pass
    def json(self): return self.payload


class GitHub:
    def __init__(self, source, owner, repo):
        self.source, self.owner, self.repo = source, owner, repo
        self.client = self
    def get(self, *_args, **_kwargs): return SearchResponse(self.source, self.owner, self.repo)
    def enrich(self, fact):
        if fact.repository_url is None: return fact
        return FactSet(fact.source_name, fact.repository_url,
                       github=GitHubFacts(f"{self.owner}/{self.repo}"),
                       github_owner_login=self.owner)


class UDD:
    def __init__(self, source): self.source = source
    def fetch(self, names):
        return {name: FactSet(name, repository_url=f"https://github.com/{self.source}/{self.source}")
                for name in names}


def test_rejected_official_claim_cannot_fall_back_to_a_plain_repository_url():
    from dividend.upstream.verifier import TRUSTED_PROJECT_SOURCES

    facts = FactSet(
        "curl", repository_url="https://github.com/curl/curl",
        github=GitHubFacts("impostor/curl"),
        official_repository_url="https://github.com/curl/curl",
        official_project_source_url=TRUSTED_PROJECT_SOURCES["curl"],
        official_provenance="fixture evidence",
    )
    result = map_source("curl", facts, OverrideConfig())
    assert result.outcome == "unmapped"
    assert result.upstream_key is None


def test_official_curl_and_systemd_links_persist_as_deterministic_mappings(tmp_path):
    for source, owner, repo in (("curl", "curl", "curl"), ("systemd", "systemd", "systemd")):
        engine = create_database_engine(f"sqlite:///{tmp_path / (source + '.db')}")
        initialize_database(engine)
        factory = create_session_factory(engine)
        repository_url = f"https://github.com/{owner}/{repo}"
        enricher = LiveEnricher(UDD(source), GitHub(source, owner, repo), SourceClient(repository_url))
        try:
            with factory() as db:
                session = SessionService.create(db, "paste", source)
                session_id = session.id
                db.commit()
            with factory() as db:
                SessionService.resolve_sources(db, session_id, enricher, OverrideConfig())
                db.commit()
            with factory() as db:
                resolution = db.scalar(select(SourceResolution).where(SourceResolution.session_id == session_id))
                project = db.scalar(select(UpstreamProject).where(UpstreamProject.session_id == session_id))
                assert resolution.method == "official_source"
                assert resolution.evidence[0]["field"] == "official.project_source"
                assert resolution.evidence[0]["value"] == TRUSTED_PROJECT_SOURCES[source]
                assert resolution.validated is True
                assert project.upstream_key == f"github.com/{owner}/{repo}"
        finally:
            engine.dispose()


def test_bash_repository_discovery_stays_unverified_without_official_source_evidence(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'bash.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            session = SessionService.create(db, "paste", "bash")
            session_id = session.id
            db.commit()
        enricher = LiveEnricher(UDD("bash"), GitHub("bash", "git", "bash"), SourceClient("https://example.test/bogus"))
        with factory() as db:
            SessionService.resolve_sources(db, session_id, enricher, OverrideConfig())
            db.commit()
        with factory() as db:
            project = db.scalar(select(UpstreamProject).where(UpstreamProject.session_id == session_id))
            assert project.upstream_key == "github.com/bash/bash"
            resolution = db.scalar(select(SourceResolution).where(SourceResolution.session_id == session_id))
            assert resolution.method == "repo_url"
            assert resolution.outcome == "resolved"
            assert resolution.validated is False
    finally:
        engine.dispose()
