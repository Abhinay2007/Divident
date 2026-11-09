import asyncio
import httpx
from sqlalchemy import select

from dividend.config import Settings
from dividend.db import create_database_engine, create_session_factory, initialize_database
from dividend.main import create_app
from dividend.models import Maintainer, Plan, PayoutBatch, PayoutItem, Session as SessionRecord, SourceResolution, UpstreamProject
from dividend.api.deps import plan_access_token
from dividend.api.deps import admin_auth
from fastapi import HTTPException


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
        config = await client.get("/config")
        assert config.status_code == 200
        assert config.json()["payments_mode"] == "fake"
        assert config.json()["fee_params"]["order_fee_pct"] == 3.49

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
            "projects": [], "unmapped": ["src-0", "src-1"], "no_upstream": [],
            "sources": [
                {"source_name": "src-0", "binary_names": ["bin-0"], "upstream_key": None, "method": "none", "outcome": "unmapped", "validated": False, "evidence": None, "repository_url": None},
                {"source_name": "src-1", "binary_names": ["bin-1"], "upstream_key": None, "method": "none", "outcome": "unmapped", "validated": False, "evidence": None, "repository_url": None},
            ],
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


def test_admin_auth_dependency_uses_constant_time_token_check(tmp_path):
    from types import SimpleNamespace
    import pytest

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings(tmp_path))))
    asyncio.run(admin_auth(request, "test-token"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(admin_auth(request, "wrong"))
    assert error.value.status_code == 401


def test_plan_routes_require_capability_and_rejected_calls_have_no_payment_side_effects(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    factory = create_session_factory(engine)
    app = create_app(settings(tmp_path), engine=engine, session_factory=factory)
    initialize_database(engine)
    with factory.begin() as db:
        session = SessionRecord(mode="upload", status="ready", source_names=[], source_count=0)
        db.add(session)
        db.flush()
        plan = Plan(session_id=session.id, state="settled", budget_cents=1000,
                    payable_total_cents=1000, reserved_total_cents=0, charge_cents=None,
                    payout_attempt=1)
        db.add(plan)
        db.flush()
        plan_id = plan.id
        batch = PayoutBatch(plan_id=plan_id, attempt=1, sender_batch_id="sender-test",
                            external_id="provider-test-batch", status="success")
        db.add(batch)
        db.flush()
        db.add(PayoutItem(batch_id=batch.id, plan_id=plan_id, upstream_key="owner/repo",
                          sender_item_id="item-test", recipient_email="private@example.test",
                          amount_cents=100, status="success"))
        db.add(Maintainer(upstream_key="owner/repo", display_name="Public Maintainer",
                          email="private@example.test", opted_in=True))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                paths = [
                    ("get", f"/api/plans/{plan_id}"),
                    ("patch", f"/api/plans/{plan_id}/budget"),
                    ("post", f"/api/plans/{plan_id}/approve"),
                    ("post", f"/api/plans/{plan_id}/capture"),
                    ("post", f"/api/plans/{plan_id}/payout"),
                    ("post", f"/api/plans/{plan_id}/payout/retry"),
                    ("post", f"/api/plans/{plan_id}/reconcile"),
                    ("get", f"/api/plans/{plan_id}/payouts"),
                    ("post", f"/api/plans/{plan_id}/cancel"),
                ]
                for method, path in paths:
                    response = await getattr(client, method)(path)
                    assert response.status_code == 401, (path, response.text)
                    assert response.json()["request_id"]
                visible = await client.get(f"/api/plans/{plan_id}/payouts", headers={
                    "X-Plan-Token": plan_access_token("test-token", plan_id),
                })
                assert visible.status_code == 200
                assert "private@example.test" not in visible.text
                assert "recipient_email" not in visible.text
                assert visible.json()["batches"][0]["items"][0]["recipient_display"] == "Public Maintainer"
                assert not app.state.provider.orders
                assert not app.state.provider.batches

    asyncio.run(exercise())
    with factory() as db:
        assert db.get(Plan, plan_id).state == "settled"
    engine.dispose()


def test_llm_kill_switch_overrides_injected_provider(tmp_path):
    from dividend.llm.groq_provider import GroqProvider

    engine = create_database_engine(f"sqlite:///{tmp_path / 'kill-switch.db'}")
    factory = create_session_factory(engine)
    configured = Settings(
        ADMIN_TOKEN="test-token", PUBLIC_BASE_URL="https://example.test",
        PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=True, LLM_KILL_SWITCH=True,
        allocation_policy_path=tmp_path / "allocation_policy.yaml",
    )
    app = create_app(configured, engine=engine, session_factory=factory,
                     llm_provider=GroqProvider("unused", "test", client=object()))
    assert app.state.llm is None
    engine.dispose()


def test_api_approval_returns_charge_error_without_hanging(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'approval.db'}")
    factory = create_session_factory(engine)
    app = create_app(settings(tmp_path), engine=engine, session_factory=factory)
    initialize_database(engine)
    with factory.begin() as db:
        session = SessionRecord(mode="upload", status="ready", source_names=[], source_count=0)
        db.add(session)
        db.flush()
        plan = Plan(
            session_id=session.id,
            state="draft",
            budget_cents=1000,
            payable_total_cents=1000,
            reserved_total_cents=0,
            est_payout_fees_cents=None,
            charge_cents=None,
        )
        db.add(plan)
        db.flush()
        plan_id = plan.id

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(f"/api/plans/{plan_id}/approve", headers={"X-Plan-Token": plan_access_token("test-token", plan_id)})
                assert response.status_code == 422
                assert response.json()["code"] == "charge_unavailable"

    asyncio.run(exercise())
    with factory() as db:
        persisted = db.get(Plan, plan_id)
        assert (persisted.state, persisted.order_attempt) == ("draft", 0)
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
                resolution.validated = True
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
        headers = {"X-Plan-Token": plan["action_token"]}
        assert plan["budget_cents"] == 1000
        assert sum(line["amount_cents"] for line in plan["lines"]) == 1000
        assert plan["charge_cents"] is None
        assert plan["est_payout_fees_cents"] == 0
        explanations = await client.get(f"/api/plans/{plan['id']}/explanations", headers=headers)
        assert explanations.status_code == 200, explanations.text
        assert len(explanations.json()["explanations"]) == len(plan["lines"])
        assert all(item["status"] == "disabled" for item in explanations.json()["explanations"])
        assert all(claim["evidence_ids"] for item in explanations.json()["explanations"]
                   for claim in item["claims"])

        adjusted = await client.patch(
            f"/api/plans/{plan['id']}/budget",
            json={"budget_cents": 1100, "evaluation_date": "2026-11-06"},
            headers=headers,
        )
        assert adjusted.status_code == 200, adjusted.text
        assert adjusted.json()["budget_cents"] == 1100
        assert sum(line["amount_cents"] for line in adjusted.json()["lines"]) == 1100

        infeasible = await client.patch(
            f"/api/plans/{plan['id']}/budget",
            json={"budget_cents": 100, "evaluation_date": "2026-11-06"},
            headers=headers,
        )
        assert infeasible.status_code == 422
        assert infeasible.json()["code"] == "allocation_error"
        assert infeasible.json()["request_id"]
        assert (await client.get(f"/api/plans/{plan['id']}", headers=headers)).json()["budget_cents"] == 1100

        approved = await client.post(f"/api/plans/{plan['id']}/approve", headers=headers)
        assert approved.status_code == 422, approved.text
        assert approved.json()["code"] == "charge_unavailable"
        assert (await client.get(f"/api/plans/{plan['id']}", headers=headers)).json()["state"] == "draft"

        still_editable = await client.patch(
            f"/api/plans/{plan['id']}/budget",
            json={"budget_cents": 1000, "evaluation_date": "2026-11-06"},
            headers=headers,
        )
        assert still_editable.status_code == 200
    asyncio.run(exercise())
    engine.dispose()
