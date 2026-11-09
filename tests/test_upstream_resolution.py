import json

from sqlalchemy import func, select

from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.enrichment.base import FactSet, GitHubFacts
from dividend.models import LLMCache, Maintainer, PayoutBatch, Session, SourceResolution, UpstreamProject
from dividend.services import PlanService, SessionService
from dividend.upstream.mapper import map_source
from dividend.upstream.normalizer import normalize
from dividend.upstream.overrides import OverrideConfig
from dividend.upstream.resolver import _prompt, cache_key, resolve
from dividend.upstream.validator import Proposal, validate


def test_normalization_and_deterministic_priority():
    assert normalize("git@github.com:Example/Project.git") == "github.com/example/project"
    facts = FactSet("pkg", "https://github.com/acme/pkg.git")
    overrides = OverrideConfig(("pkg",), {"family/key": ("other",)})
    assert map_source("pkg", facts, overrides).outcome == "no_upstream"
    assert map_source("other", facts, overrides).upstream_key == "family/key"
    assert map_source("pkg2", facts, OverrideConfig()).upstream_key == "github.com/acme/pkg"
    assert map_source("missing", FactSet("missing"), OverrideConfig()).outcome == "unmapped"


def test_validator_requires_repository_evidence_to_derive_identity():
    facts = FactSet("pkg", "https://github.com/acme/pkg.git", homepage="https://acme.example/project")
    valid = Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "udd.repository", "value": facts.repository_url}])
    assert validate(valid, facts)
    assert not validate(Proposal("pkg", "github.com/fake/pkg", "pkg", valid.evidence), facts)
    assert not validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "README", "value": "github.com/acme/pkg"}]), facts)
    assert not validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [{"field": "udd.repository", "value": "https://evil.example/pkg"}]), facts)
    assert validate(Proposal("pkg", "github.com/acme/pkg", "pkg", [
        {"field": "udd.repository", "value": facts.repository_url},
        {"field": "udd.homepage", "value": facts.homepage},
    ]), facts)


def test_resolver_never_sends_udd_contact_to_groq_or_uses_it_in_cache_key():
    facts = FactSet("pkg", repository_url="https://github.com/acme/pkg",
                    contact="private-maintainer@example.test")
    prompt = _prompt([facts])
    changed_contact = FactSet("pkg", repository_url=facts.repository_url,
                              contact="another-private@example.test")
    assert "private-maintainer@example.test" not in prompt
    assert "udd.contact" not in prompt
    assert cache_key(facts, "model") == cache_key(changed_contact, "model")


def test_homepage_and_unrelated_evidence_cannot_derive_upstream_identity():
    homepage = "https://acme.example/project"
    facts = FactSet("pkg", homepage=homepage, contact="team@example.org", github_owner_login="acme")
    key = normalize(homepage)
    assert not validate(Proposal("pkg", key, "project", [{"field": "udd.homepage", "value": homepage}]), facts)
    combined = Proposal("pkg", key, "project", [
        {"field": "udd.homepage", "value": homepage},
        {"field": "udd.contact", "value": facts.contact},
        {"field": "github.owner_login", "value": facts.github_owner_login},
    ])
    assert not validate(combined, facts)
    assert not validate(combined, facts, {key})
    assert not validate(Proposal("pkg", key, "project", [{"field": "udd.homepage", "value": homepage}]),
                        facts, {key})


def test_github_repository_evidence_and_existing_upstream_keys_remain_valid():
    facts = FactSet("pkg", github=GitHubFacts("acme/pkg"))
    known_key = "github.com/acme/pkg"
    proposal = Proposal("pkg", known_key, "pkg", [{"field": "github.full_name", "value": "acme/pkg"}])
    assert validate(proposal, facts)

    family_fact = FactSet("fork", github=GitHubFacts("fork-owner/fork"),
                          github_parent_full_name="acme/pkg")
    family_proposal = Proposal("fork", known_key, "pkg", [
        {"field": "github.parent_full_name", "value": "acme/pkg"},
    ])
    assert validate(family_proposal, family_fact, {known_key})

    overrides = OverrideConfig(family_patterns={"family/key": ("pkg-*",)})
    mapping = map_source("pkg-member", FactSet("pkg-member"), overrides)
    assert mapping.method == "override" and mapping.upstream_key == "family/key"


def test_llm_batches_and_retries_only_rejected_subset_with_limit():
    facts = [FactSet(f"pkg-{i}", repository_url=f"https://git.example.org/pkg-{i}",
                     homepage=f"https://example.org/{i}") for i in range(21)]

    class Fake:
        def __init__(self):
            self.calls = []

        def structured_call(self, prompt, schema, timeout=30):
            import json
            batch = json.loads(prompt.split("\n", 1)[1])
            self.calls.append([fact["source_name"] for fact in batch])
            return {"proposals": [{"source_name": f["source_name"], "upstream_key": f"git.example.org/{f['source_name']}",
                                   "display_name": "project", "evidence": [
                                       {"field": "udd.repository", "value": f["udd.repository"]},
                                       {"field": "udd.homepage", "value": f["udd.homepage"]}]}
                                  for f in batch if f["source_name"] != "pkg-0"]}

    provider = Fake()
    got = resolve(facts, provider)
    assert len(got) == 20
    assert max(map(len, provider.calls)) <= 10
    assert provider.calls == [[f.source_name for f in facts[:10]], ["pkg-0"], ["pkg-0"],
                              [f.source_name for f in facts[10:20]], [f.source_name for f in facts[20:]]]
    assert sum(call.count("pkg-0") for call in provider.calls) == 3


def test_validated_resolution_cache_hits_and_model_invalidates(tmp_path):
    from dividend.db import create_session_factory, initialize_database

    engine = create_database_engine(f"sqlite:///{tmp_path / 'llm-cache.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    fact = FactSet("pkg", repository_url="https://git.example.org/project", homepage="https://example.org/project")

    class Fake:
        def __init__(self): self.calls = 0
        def structured_call(self, prompt, schema, timeout=30):
            self.calls += 1
            return {"proposals": [{"source_name": "pkg", "upstream_key": "git.example.org/project",
                "display_name": "project", "evidence": [
                    {"field": "udd.repository", "value": fact.repository_url},
                    {"field": "udd.homepage", "value": fact.homepage}]}]}

    try:
        with factory() as db:
            first = Fake()
            stats = {}
            get_cache = lambda item, model: (cached.response if (cached := db.get(LLMCache, cache_key(item, model))) else None)
            set_cache = lambda item, model, response: db.merge(LLMCache(key=cache_key(item, model), response=response))
            assert len(resolve([fact], first, model="model-a", cache_stats=stats,
                               cache_get=get_cache, cache_set=set_cache)) == 1
            assert stats["cache_hits"] == 0 and first.calls == 1
            db.commit()
        with factory() as db:
            unused = Fake()
            stats = {}
            get_cache = lambda item, model: (cached.response if (cached := db.get(LLMCache, cache_key(item, model))) else None)
            set_cache = lambda item, model, response: db.merge(LLMCache(key=cache_key(item, model), response=response))
            assert len(resolve([fact], unused, model="model-a", cache_stats=stats,
                               cache_get=get_cache, cache_set=set_cache)) == 1
            assert stats["cache_hits"] == 1 and unused.calls == 0
        with factory() as db:
            changed_model = Fake()
            assert len(resolve([fact], changed_model, model="model-b", cache_get=get_cache,
                               cache_set=set_cache)) == 1
            assert changed_model.calls == 1
    finally:
        engine.dispose()


def test_invalid_proposal_is_never_cached(tmp_path):
    from dividend.db import create_session_factory, initialize_database

    engine = create_database_engine(f"sqlite:///{tmp_path / 'invalid-cache.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    fact = FactSet("pkg")

    class Invalid:
        def structured_call(self, prompt, schema, timeout=30):
            return {"proposals": [{"source_name": "pkg", "upstream_key": "fake.invalid/pkg",
                "display_name": "pkg", "evidence": [{"field": "udd.homepage", "value": "https://fake.invalid/pkg"}]}]}

    try:
        with factory() as db:
            get_cache = lambda item, model: (cached.response if (cached := db.get(LLMCache, cache_key(item, model))) else None)
            set_cache = lambda item, model, response: db.merge(LLMCache(key=cache_key(item, model), response=response))
            assert resolve([fact], Invalid(), model="model", cache_get=get_cache,
                           cache_set=set_cache) == []
            assert db.scalar(select(LLMCache)) is None
    finally:
        engine.dispose()


def test_invalid_ai_mapping_stays_unmapped_and_cannot_touch_payee_or_payout_data(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'llm-isolation.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)

    class InvalidProvider:
        prompt = ""
        def structured_call(self, prompt, schema, timeout=30):
            self.prompt = prompt
            return {"proposals": [{"source_name": "candidate", "upstream_key": "fake.invalid/project",
                "display_name": "project", "evidence": [{"field": "udd.homepage",
                "value": "https://fake.invalid/project"}]}]}

    provider = InvalidProvider()
    try:
        with factory() as db:
            session = SessionService.create(db, "paste", "candidate")
            session_id = session.id
            db.add(Maintainer(upstream_key="example.org/project", display_name="Maintainer",
                              email="private@example.org", opted_in=False))
            db.commit()

        class Enricher:
            def fetch(self, names):
                return [FactSet("candidate", homepage="https://fake.invalid/project")]

        with factory() as db:
            SessionService.resolve_sources(db, session_id, Enricher(), OverrideConfig(), provider)
            db.commit()
        with factory() as db:
            resolution = db.scalar(select(SourceResolution).where(
                SourceResolution.session_id == session_id, SourceResolution.source_name == "candidate"))
            maintainer = db.scalar(select(Maintainer).where(Maintainer.upstream_key == "example.org/project"))
            assert resolution.outcome == "unmapped" and resolution.upstream_key is None
            assert maintainer.email == "private@example.org" and maintainer.opted_in is False
            assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 0
            assert "private@example.org" not in provider.prompt
            assert "payment" not in provider.prompt.lower()
    finally:
        engine.dispose()


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
        def execute(self, query, params=None):
            if query.startswith("SET LOCAL"):
                assert query == "SET LOCAL client_encoding TO 'UTF8'"
            else:
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
            from dividend.allocation.engine import AllocationError
            import pytest
            with pytest.raises(AllocationError, match="no eligible"):
                PlanService.assemble_allocation_inputs(db, session_id, __import__("datetime").date(2026, 1, 1))
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
