import asyncio

import httpx
import pytest
from sqlalchemy import select

from dividend.api.deps import plan_access_token
from dividend.config import Settings
from dividend.db import create_database_engine, create_session_factory
from dividend.main import create_app
from dividend.models import Maintainer, Session as SessionRecord, SourceResolution, UpstreamProject


class RankingLLM:
    def __init__(self, invalid=False):
        self.invalid = invalid
        self.prompt = ""

    def structured_call(self, prompt, schema, timeout=30):
        self.prompt = prompt
        ids = schema["properties"]["claim_ids"]["items"]["enum"]
        return {"claim_ids": ["invented"] if self.invalid else list(reversed(ids))}


@pytest.mark.parametrize("invalid", [False, True])
def test_explanations_use_frozen_evidence_and_groq_cannot_write_claims(tmp_path, invalid):
    engine = create_database_engine(f"sqlite:///{tmp_path / f'explanations-{invalid}.db'}")
    factory = create_session_factory(engine)
    llm = RankingLLM(invalid=invalid)
    settings = Settings(ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
                        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True, GROQ_API_KEY="test-key",
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
                result = response.json()["explanations"][0]
                assert result["status"] == ("invalid_response_fallback" if invalid else "groq_ordered")
                assert all(claim["text"] != "invented" for claim in result["claims"])
                assert any("cannot be established" in claim["text"] for claim in result["claims"])
                assert "private@example.test" not in llm.prompt
                assert "Private Test Name" not in llm.prompt
                assert "email" not in llm.prompt.lower()
                references = {evidence["id"] for evidence in result["evidence"]}
                assert all(set(claim["evidence_ids"]).issubset(references | {
                    "MISSING:project-description", "MISSING:contribution-activity"
                }) for claim in result["claims"])

    asyncio.run(exercise())
    engine.dispose()
