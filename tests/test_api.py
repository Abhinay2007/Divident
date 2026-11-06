import asyncio
import httpx
from sqlalchemy import select

from dividend.config import Settings
from dividend.db import create_database_engine, create_session_factory
from dividend.main import create_app
from dividend.models import Session as SessionRecord, SourceResolution, UpstreamProject


def settings(tmp_path):
    return Settings(
        ADMIN_TOKEN="test-token",
        PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE="fake",
        LLM_LIVE_ENABLED=False,
        allocation_policy_path=tmp_path / "allocation_policy.yaml",
    )


def upload_payload(count=2):
    sources = {f"src-{index}": [f"bin-{index}"] for index in range(count)}
    return {
        "schema": 1,
        "origin": "api-test",
        "binary_count": count,
        "source_count": count,
        "sources": sources,
    }


def test_api_session_validation_request_id_and_health_startup(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'api.db'}")
    factory = create_session_factory(engine)
    app = create_app(settings(tmp_path), engine=engine, session_factory=factory)
    async def exercise():
      async with app.router.lifespan_context(app):
       async with httpx.AsyncClient(
           transport=httpx.ASGITransport(app=app), base_url="http://test"
       ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"db": "ok", "provider_mode": "fake", "fixtures_loaded": True}
        assert health.headers.get("x-request-id")

        response = await client.post(
            "/api/sessions",
            json={"mode": "upload", "payload": upload_payload()},
            headers={"X-Request-ID": "9cdf6a08-1c3e-4bb5-9a62-93447a5f0092"},
        )
        assert response.status_code == 201
        created = response.json()
        assert created["status"] == "created"
        assert created["source_count"] == 2
        assert response.headers["x-request-id"] == "9cdf6a08-1c3e-4bb5-9a62-93447a5f0092"
        session_id = created["id"]

        fetched = await client.get(f"/api/sessions/{session_id}")
        assert fetched.status_code == 200
        projects = await client.get(f"/api/sessions/{session_id}/projects")
        assert projects.status_code == 200
        assert projects.json() == {
            "projects": [], "unmapped": ["src-0", "src-1"], "no_upstream": []
        }

        bad = await client.post(
            "/api/sessions",
            json={"mode": "upload", "payload": {**upload_payload(), "binary_count": 3}},
        )
        assert bad.status_code == 422
        assert bad.json()["error"] == "validation_error"
        assert bad.json()["code"] == "request_validation_error"
        assert bad.json()["request_id"]

        too_large = await client.post(
            "/api/sessions",
            content=b" " * 1_000_001,
            headers={"content-type": "application/json"},
        )
        assert too_large.status_code == 413
        assert too_large.json()["code"] == "request_too_large"
    asyncio.run(exercise())
    engine.dispose()


def test_end_to_end_api_intake_to_plan_budget_adjustment_and_approval(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'flow.db'}")
    factory = create_session_factory(engine)
    app = create_app(settings(tmp_path), engine=engine, session_factory=factory)
    async def exercise():
      async with app.router.lifespan_context(app):
       async with httpx.AsyncClient(
           transport=httpx.ASGITransport(app=app), base_url="http://test"
       ) as client:
        response = await client.post(
            "/api/sessions",
            json={"mode": "upload", "payload": upload_payload(7)},
        )
        assert response.status_code == 201
        session_id = response.json()["id"]

        # Deterministic test fixture stands in for the absent T4 producer. It
        # does not assert or implement production upstream resolution behavior.
        with factory.begin() as db:
            session = db.get(SessionRecord, session_id)
            session.status = "ready"
            for index in range(7):
                source_name = f"src-{index}"
                resolution = db.scalar(
                    select(SourceResolution).where(
                        SourceResolution.session_id == session_id,
                        SourceResolution.source_name == source_name,
                    )
                )
                key = f"fixture/project-{index}"
                resolution.method = "override"
                resolution.upstream_key = key
                db.add(UpstreamProject(
                    session_id=session_id,
                    upstream_key=key,
                    display_name=f"Fixture {index}",
                    repository_url=f"https://example.test/{index}",
                    binary_count=1,
                    source_names=[source_name],
                    source_facts={source_name: {
                        "source_name": source_name,
                        "repository_url": f"https://example.test/{index}",
                        "homepage": None,
                        "donation": None,
                        "github": None,
                        "partial": False,
                    }},
                    need_signals={
                        "archived": False, "last_commit": None,
                        "committers_12m": None, "funding_yml": None,
                        "partial": False,
                    },
                ))

        plan_response = await client.post(
            f"/api/sessions/{session_id}/plans",
            json={"budget_cents": 1000, "evaluation_date": "2026-11-06"},
        )
        assert plan_response.status_code == 201, plan_response.text
        plan = plan_response.json()
        assert plan["budget_cents"] == 1000
        assert sum(line["amount_cents"] for line in plan["lines"]) == 1000
        assert plan["charge_cents"] is None
        assert plan["est_payout_fees_cents"] is None

        adjusted = await client.patch(
            f"/api/plans/{plan['id']}/budget",
            json={"budget_cents": 1100, "evaluation_date": "2026-11-06"},
        )
        assert adjusted.status_code == 200, adjusted.text
        assert adjusted.json()["budget_cents"] == 1100
        assert sum(line["amount_cents"] for line in adjusted.json()["lines"]) == 1100

        approved = await client.post(f"/api/plans/{plan['id']}/approve")
        assert approved.status_code == 422, approved.text
        assert approved.json()["code"] == "charge_unavailable"
        assert (await client.get(f"/api/plans/{plan['id']}")).json()["state"] == "draft"

        still_editable = await client.patch(
            f"/api/plans/{plan['id']}/budget",
            json={"budget_cents": 1000, "evaluation_date": "2026-11-06"},
        )
        assert still_editable.status_code == 200
    asyncio.run(exercise())
    engine.dispose()
