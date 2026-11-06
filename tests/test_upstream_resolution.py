import json

from sqlalchemy import select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.enrichment.base import FactSet
from dividend.models import Session, SourceResolution, UpstreamProject
from dividend.services import PlanService, SessionService
from dividend.upstream.mapper import map_source
from dividend.upstream.normalizer import normalize
from dividend.upstream.overrides import OverrideConfig
from dividend.upstream.resolver import resolve
from dividend.upstream.validator import Proposal, validate


def test_normalization_and_deterministic_priority():
    assert normalize("git@github.com:Example/Project.git") == "github.com/example/project"
    facts = FactSet("pkg", "https://github.com/acme/pkg.git")
    overrides = OverrideConfig(("pkg",), {"family/key": ("other",)})
    assert map_source("pkg", facts, overrides).outcome == "no_upstream"
    assert map_source("other", facts, overrides).upstream_key == "family/key"
    assert map_source("pkg2", facts, OverrideConfig()).upstream_key == "github.com/acme/pkg"
    assert map_source("missing", FactSet("missing"), OverrideConfig()).outcome == "unmapped"


def test_validator_requires_allowlisted_exact_evidence_and_derived_key():
    facts = FactSet("pkg", "https://github.com/acme/pkg.git", homepage="https://acme.example/project")
    valid = Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "udd.repository", "value": facts.repository_url}])
    assert validate(valid, facts)
    assert not validate(Proposal("pkg", "github.com/fake/pkg", "pkg", valid.evidence), facts)
    assert not validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "README", "value": "github.com/acme/pkg"}]), facts)
    assert not validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "udd.repository", "value": "https://evil.example/pkg"}]), facts)
    assert not validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [
        {"field": "udd.repository", "value": facts.repository_url},
        {"field": "udd.homepage", "value": facts.homepage},
    ]), facts)
    assert validate(Proposal("pkg", "family/key", "pkg", [{"field": "udd.homepage", "value": facts.homepage}]), facts, {"family/key"})


def test_llm_batches_and_retries_only_rejected_subset_with_limit():
    facts = [FactSet(f"pkg-{i}", homepage=f"https://example.org/{i}") for i in range(21)]

    class Fake:
        def __init__(self):
            self.calls = []

        def structured_call(self, prompt, schema, timeout=30):
            import json
            batch = json.loads(prompt.split("\n", 1)[1])
            self.calls.append([fact["source_name"] for fact in batch])
            return {"proposals": [{"source_name": f["source_name"], "upstream_key": f"example.org/{f['source_name'].split('-')[1]}",
                                   "display_name": "project", "evidence": [{"field": "udd.homepage", "value": f["udd.homepage"]}]}
                                  for f in batch if f["source_name"] != "pkg-0"]}

    provider = Fake()
    got = resolve(facts, provider)
    assert len(got) == 20
    assert max(map(len, provider.calls)) <= 10
    assert provider.calls == [[f.source_name for f in facts[:10]], ["pkg-0"], ["pkg-0"],
                              [f.source_name for f in facts[10:20]], [f.source_name for f in facts[20:]]]
    assert sum(call.count("pkg-0") for call in provider.calls) == 3


def test_eval_fixture_has_at_least_30_contract_cases_and_validator_rejections():
    fixture = json.loads(open("data/upstream_eval_cases.json", encoding="utf-8").read())
    assert len(fixture["cases"]) >= 30
    rejected = 0
    resolved = 0
    for case in fixture["cases"]:
        facts = FactSet(case["source"], case["repository_url"])
        if case["expected_key"]:
            assert normalize(case["repository_url"]) == case["expected_key"]
            resolved += 1
        else:
            bad = Proposal(case["source"], "fake.invalid/repo", "unknown",
                           [{"field": "udd.repository", "value": "https://fake.invalid/repo"}])
            rejected += not validate(bad, facts)
    assert rejected >= 2
    assert resolved == 27


def test_udd_and_github_clients_return_allowlisted_structured_facts():
    from dividend.enrichment.github_client import GitHubClient
    from dividend.enrichment.udd_client import UDDClient

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, query, params):
            assert "upstream_metadata" in query and params == (["pkg"],)
        def fetchall(self): return [("pkg", "https://example.org", "https://github.com/acme/pkg.git", None)]

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def cursor(self): return Cursor()

    facts = UDDClient("dsn", lambda dsn: Connection()).fetch(["pkg"])["pkg"]
    class Response:
        status_code = 404
        def __init__(self, url): self.url = url
        def raise_for_status(self): pass
        def json(self):
            if self.url.endswith("/repos/acme/pkg"):
                return {"full_name": "acme/pkg", "archived": False, "owner": {"login": "acme"}}
            if "/commits" in self.url:
                return [{"commit": {"author": {"date": "2026-01-01T00:00:00Z"}}}]
            return {}
    class HTTP:
        def get(self, url, **kwargs):
            return Response(url)
    enriched = GitHubClient("token", HTTP()).enrich(facts)
    assert enriched.github.full_name == "acme/pkg"
    assert enriched.github_owner_login == "acme"
    assert enriched.github.last_commit == "2026-01-01T00:00:00Z"


def test_resolution_persists_and_plan_service_consumes_grouped_sources(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'resolution.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            session = SessionService.create(db, "upload", {
                "schema": 1, "origin": "test", "source_count": 2, "binary_count": 3,
                "sources": {"pkg-a": ["bin-a", "bin-a-doc"], "pkg-b": ["bin-b"]},
            })
            session_id = session.id
            db.commit()
        with factory() as db:
            SessionService.mark_resolving(db, session_id)
            db.commit()
        class Enricher:
            def fetch(self, names):
                return [FactSet(name, "https://github.com/acme/shared.git") for name in names]
        with factory() as db:
            SessionService.resolve_sources(db, session_id, Enricher(), OverrideConfig())
            db.commit()
        with factory() as db:
            project = db.scalar(select(UpstreamProject).where(UpstreamProject.session_id == session_id))
            rows = list(db.scalars(select(SourceResolution).where(SourceResolution.session_id == session_id)))
            assert project.upstream_key == "github.com/acme/shared"
            assert project.source_names == ["pkg-a", "pkg-b"]
            assert project.binary_count == 3
            assert set(project.source_facts) == {"pkg-a", "pkg-b"}
            assert all(row.outcome == "resolved" and row.method == "repo_url" for row in rows)
            inputs = PlanService.assemble_allocation_inputs(db, session_id, __import__("datetime").date(2026, 1, 1))
            assert len(inputs.projects) == 1
            assert inputs.projects[0].binary_count == 3
    finally:
        engine.dispose()


def test_unmapped_and_intentional_no_upstream_are_persisted_separately(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'outcomes.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            session = SessionService.create(db, "paste", "keep\nexcluded\nunknown")
            session_id = session.id
            db.commit()
        class Enricher:
            def fetch(self, names):
                return [FactSet(name, "https://github.com/acme/keep.git" if name == "keep" else None)
                        for name in names]
        with factory() as db:
            SessionService.resolve_sources(db, session_id, Enricher(), OverrideConfig(("excluded",), {}))
            db.commit()
        with factory() as db:
            rows = {row.source_name: row for row in db.scalars(
                select(SourceResolution).where(SourceResolution.session_id == session_id))}
            projects = SessionService.get_projects(db, session_id)
            assert rows["excluded"].outcome == "no_upstream"
            assert rows["unknown"].outcome == "unmapped"
            assert projects.no_upstream == ["excluded"]
            assert projects.unmapped == ["unknown"]
            assert [item.upstream_key for item in projects.projects] == ["github.com/acme/keep"]
    finally:
        engine.dispose()


def test_malformed_candidate_is_isolated_from_valid_resolution(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'isolated.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            session = SessionService.create(db, "paste", "good\nbad")
            session_id = session.id
            db.commit()
        class Enricher:
            def fetch(self, names):
                return [FactSet("good", "https://github.com/acme/good.git"),
                        FactSet("bad", repository_url=123)]
        with factory() as db:
            SessionService.resolve_sources(db, session_id, Enricher(), OverrideConfig())
            db.commit()
        with factory() as db:
            result = SessionService.get_projects(db, session_id)
            assert [project.upstream_key for project in result.projects] == ["github.com/acme/good"]
            assert result.unmapped == ["bad"]
    finally:
        engine.dispose()
