import asyncio

import httpx
from sqlalchemy import select

from dividend.api.deps import plan_access_token
from dividend.config import Settings
from dividend.db import create_database_engine, create_session_factory
from dividend.main import create_app
from dividend.models import Maintainer, Session as SessionRecord, SourceResolution, UpstreamProject


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def structured_call(self, prompt, schema, timeout=30):
        self.calls += 1
        return {}


def test_multi_line_explanations_are_deterministic_without_modal_calls(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'explanations.db'}")
    factory = create_session_factory(engine)
    llm = CountingLLM()
    settings = Settings(ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
                        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True,
                        MODAL_PROXY_TOKEN_ID="test-id", MODAL_PROXY_TOKEN_SECRET="test-secret",
                        DEMO_SEED_ON_BOOT=False,
                        allocation_policy_path=tmp_path / "allocation_policy.yaml")
    app = create_app(settings, engine=engine, session_factory=factory, llm_provider=llm)

    async def exercise():
        async with app.router.lifespan_context(app):
            with factory.begin() as db:
                names = [f"pkg-{index}" for index in range(7)]
                session = SessionRecord(mode="upload", status="ready", source_names=names,
                    source_binaries={name: [f"bin-{index}"] for index, name in enumerate(names)},
                    source_count=len(names), binary_count=len(names))
                db.add(session)
                db.flush()
                for index, source_name in enumerate(names):
                    key = f"github.com/project-{index}/repo"
                    repository = f"https://{key}"
                    db.add(SourceResolution(session_id=session.id, source_name=source_name, upstream_key=key,
                        method="official_source", outcome="resolved", validated=True, cached=False,
                        evidence=[{"field": "official.project_source", "value": "https://example.test/source"},
                                  {"field": "official.provenance", "value": "demo_fixture"}]))
                    db.add(UpstreamProject(session_id=session.id, upstream_key=key,
                        display_name=f"Project {index}", repository_url=repository, binary_count=1,
                        source_names=[source_name], source_facts={source_name: {"source_name": source_name,
                        "repository_url": repository, "homepage": None, "donation": None,
                        "github": None, "partial": True}}, need_signals={"archived": None,
                        "last_commit": None, "committers_12m": None, "funding_yml": None, "partial": True}))
                db.add(Maintainer(upstream_key="github.com/project-0/repo", display_name="Private Test Name",
                                  email="private@example.test", opted_in=True))
                db.flush()
                session_id = session.id
            with factory() as db:
                from dividend.services import PlanService
                plan = PlanService.create(db, session_id, 10000, __import__("datetime").date(2026, 11, 9))
                plan_id = plan.id
                db.commit()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(f"/api/plans/{plan_id}/explanations",
                    headers={"X-Plan-Token": plan_access_token("test-token", plan_id)})
                assert response.status_code == 200, response.text
                results = response.json()["explanations"]
                assert len(results) == 7
                assert all(result["status"] == "disabled" for result in results)
                result = results[0]
                assert any("cannot be established" in claim["text"] for claim in result["claims"])
                assert llm.calls == 0
                references = {evidence["id"] for evidence in result["evidence"]}
                assert all(set(claim["evidence_ids"]).issubset(references | {
                    "MISSING:project-description", "MISSING:contribution-activity"
                }) for claim in result["claims"])

    asyncio.run(exercise())
    engine.dispose()
