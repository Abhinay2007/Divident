import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from dividend.config import Settings
from dividend.db import Base, create_database_engine, create_session_factory, initialize_database
from dividend.main import create_app
from dividend.models import Maintainer, MaintainerClaim
from dividend.services.maintainer_claim_service import ClaimError, MaintainerClaimService, _hash
from dividend.api.middleware import RequestContextMiddleware


def setup(tmp_path, count=1):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'claims.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    ids = []
    with factory.begin() as db:
        for i in range(count):
            maintainer = Maintainer(upstream_key=f"owner/repo{i}", display_name=f"M{i}",
                                    email=f"m{i}@example.test", market_code="US")
            db.add(maintainer)
            db.flush()
            ids.append(maintainer.id)
    return engine, factory, ids


def claim_session(service, db, recipient_id):
    url = service.issue_invitation(db, recipient_id)
    # The credential is deliberately in the URL fragment and never sent to the server.
    token = url.split("#token=", 1)[1]
    session, _ = service.redeem_invitation(db, token)
    db.commit()
    return _hash(session)


def state_from(service, db, session_hash):
    url = service.authorization_url(db, session_hash)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    claim = db.scalar(select(MaintainerClaim).where(MaintainerClaim.session_token_hash == session_hash))
    expected = base64.urlsafe_b64encode(hashlib.sha256(
        claim.oauth_code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    return query["state"][0]


def mock_github(user_id=123456, login="maintainer"):
    def respond(request):
        if request.url.path == "/login/oauth/access_token":
            assert request.headers["accept"] == "application/json"
            form = parse_qs(request.content.decode())
            assert form["code_verifier"]
            assert "scope" not in form
            return httpx.Response(200, json={"access_token": "secret-test-token", "scope": ""})
        if request.url.path == "/user":
            assert request.headers["authorization"] == "Bearer secret-test-token"
            return httpx.Response(200, json={"id": user_id, "login": login,
                                             "email": "ignored@example.test"})
        return httpx.Response(404)
    return httpx.MockTransport(respond)


def test_successful_claim_uses_authenticated_numeric_id_and_pkce(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    service = MaintainerClaimService("client", "server-secret", "https://example.test",
                                     transport=mock_github())
    with factory() as db:
        session_hash = claim_session(service, db, recipient_id)
        state = state_from(service, db, session_hash)
        result = asyncio.run(service.callback(db, session_hash, state, "oauth-code", None))
        assert result == "verified"
        claim = db.scalar(select(MaintainerClaim).where(MaintainerClaim.recipient_id == recipient_id))
        assert (claim.github_user_id, claim.github_login, claim.status) == (123456, "maintainer", "verified")
        assert claim.verified_at is not None
        assert not hasattr(claim, "access_token")
    engine.dispose()


def test_wrong_state_expired_state_and_repeated_claim_are_rejected(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    service = MaintainerClaimService("client", "secret", "https://example.test", transport=mock_github())
    with factory() as db:
        session_hash = claim_session(service, db, recipient_id)
        state = state_from(service, db, session_hash)
        with pytest.raises(ClaimError, match="oauth_state_invalid"):
            asyncio.run(service.callback(db, session_hash, "wrong", "code", None))
        claim = db.scalar(select(MaintainerClaim))
        claim.oauth_state_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(ClaimError, match="oauth_state_invalid"):
            asyncio.run(service.callback(db, session_hash, state, "code", None))
    engine.dispose()


def test_expired_invitation_cannot_create_a_claim_session(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    service = MaintainerClaimService("client", "secret", "https://example.test")
    with factory() as db:
        url = service.issue_invitation(db, recipient_id)
        token = url.split("#token=", 1)[1]
        claim = db.scalar(select(MaintainerClaim))
        claim.invitation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(ClaimError, match="claim_invitation_invalid"):
            service.redeem_invitation(db, token)
    engine.dispose()


def test_duplicate_identity_conflict_and_access_denied_callback(tmp_path):
    engine, factory, (first_id, second_id) = setup(tmp_path, 2)
    service = MaintainerClaimService("client", "secret", "https://example.test", transport=mock_github())
    with factory() as db:
        first_session = claim_session(service, db, first_id)
        first_state = state_from(service, db, first_session)
        assert asyncio.run(service.callback(db, first_session, first_state, "code", None)) == "verified"
        with pytest.raises(ClaimError, match="maintainer_already_claimed"):
            service.issue_invitation(db, first_id)

        second_session = claim_session(service, db, second_id)
        second_state = state_from(service, db, second_session)
        assert asyncio.run(service.callback(db, second_session, second_state, "code", None)) == "account_conflict"
        second_state = state_from(service, db, second_session)
        assert asyncio.run(service.callback(db, second_session, second_state, None, "access_denied")) == "denied"
        second = db.scalar(select(MaintainerClaim).where(MaintainerClaim.recipient_id == second_id))
        assert second.status == "pending" and second.github_user_id is None
    engine.dispose()


def test_invalid_code_and_revoked_token_do_not_verify(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    async def denied(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(401)
    service = MaintainerClaimService("client", "secret", "https://example.test",
                                     transport=httpx.MockTransport(denied))
    with factory() as db:
        session_hash = claim_session(service, db, recipient_id)
        state = state_from(service, db, session_hash)
        assert asyncio.run(service.callback(db, session_hash, state, "invalid", None)) == "authorization_failed"
        state = state_from(service, db, session_hash)
        assert asyncio.run(service.callback(db, session_hash, state, "code", None)) == "authorization_failed"
        assert db.scalar(select(MaintainerClaim)).status == "pending"
    engine.dispose()


def test_github_revoked_token_is_reported_without_claiming_identity(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    async def revoked(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "revoked-token", "scope": ""})
        return httpx.Response(401, json={"message": "Bad credentials"})
    service = MaintainerClaimService("client", "secret", "https://example.test",
                                     transport=httpx.MockTransport(revoked))
    with factory() as db:
        session_hash = claim_session(service, db, recipient_id)
        state = state_from(service, db, session_hash)
        assert asyncio.run(service.callback(db, session_hash, state, "code", None)) == "authorization_revoked"
        assert db.scalar(select(MaintainerClaim)).github_user_id is None
    engine.dispose()


def test_github_token_with_extra_scopes_is_rejected(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    revoked = []
    async def scoped(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "not-used", "scope": "repo"})
        assert request.method == "DELETE"
        assert request.url.path == "/applications/client/token"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.read().find(b"not-used") >= 0
        revoked.append(True)
        return httpx.Response(204)
    service = MaintainerClaimService("client", "secret", "https://example.test",
                                     transport=httpx.MockTransport(scoped))
    with factory() as db:
        session_hash = claim_session(service, db, recipient_id)
        state = state_from(service, db, session_hash)
        assert asyncio.run(service.callback(db, session_hash, state, "code", None)) == "permissions_rejected"
        assert db.scalar(select(MaintainerClaim)).github_user_id is None
        assert revoked == [True]
    engine.dispose()


def test_oauth_callback_query_is_removed_before_application_access_logging():
    captured = {}
    async def app(scope, receive, send):
        captured.update(scope)
    middleware = RequestContextMiddleware(app)
    async def exercise():
        await middleware({"type": "http", "path": "/api/maintainer-claims/oauth/callback",
                          "query_string": b"code=private-code&state=private-state&error=",
                          "headers": []}, None, None)
    asyncio.run(exercise())
    assert captured["query_string"] == b""
    assert captured["github_oauth_callback"] == {
        "state": "private-state", "code": "private-code", "error": None,
    }


def test_invitation_is_one_use_and_claim_routes_require_authentication(tmp_path):
    engine, factory, (recipient_id,) = setup(tmp_path)
    app = create_app(Settings(ADMIN_TOKEN="admin", PUBLIC_BASE_URL="http://test",
                              PAYMENTS_MODE="fake", LLM_LIVE_ENABLED=False,
                              GITHUB_OAUTH_CLIENT_ID="client", GITHUB_OAUTH_CLIENT_SECRET="secret"),
                     engine=engine, session_factory=factory)
    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/api/maintainer-claims/me")).status_code == 401
                assert (await client.post("/api/maintainer-claims/oauth/start",
                                          headers={"Origin": "http://test"})).status_code == 401
                assert (await client.post(f"/api/admin/maintainer-claims/{recipient_id}/invite")).status_code == 401
                invite = await client.post(f"/api/admin/maintainer-claims/{recipient_id}/invite",
                                           headers={"X-Admin-Token": "admin"})
                assert invite.status_code == 200
                token = invite.json()["claim_url"].split("#token=", 1)[1]
                headers = {"Origin": "http://test"}
                forged = await client.post("/api/maintainer-claims/session",
                                           json={"token": token, "github_user_id": 999}, headers=headers)
                assert forged.status_code == 422
                redeemed = await client.post("/api/maintainer-claims/session", json={"token": token}, headers=headers)
                assert redeemed.status_code == 200 and redeemed.json()["status"] == "pending"
                assert token not in redeemed.text
                assert "httponly" in redeemed.headers["set-cookie"].lower()
                assert "samesite=lax" in redeemed.headers["set-cookie"].lower()
                status = await client.get("/api/maintainer-claims/me")
                assert status.status_code == 200
                assert (await client.post("/api/maintainer-claims/session", json={"token": token}, headers=headers)).status_code == 401
                started = await client.post("/api/maintainer-claims/oauth/start", headers=headers)
                assert started.status_code == 200
                assert "code_challenge_method=S256" in started.json()["authorization_url"]
                callback = await client.get("/api/maintainer-claims/oauth/callback?code=private-code&state=wrong",
                                            follow_redirects=False)
                assert callback.status_code == 303
                assert callback.headers["location"].endswith("/claim?result=authorization_failed")
                assert "private-code" not in callback.headers["location"]
    asyncio.run(exercise())
    engine.dispose()


def test_additive_claim_table_creation_preserves_maintainer_rows(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-claims.db'}")
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                              if table.name != "maintainer_claims"])
    factory = create_session_factory(engine)
    with factory.begin() as db:
        maintainer = Maintainer(upstream_key="owner/old", display_name="Existing",
                                email="existing@example.test")
        db.add(maintainer)
        db.flush()
        recipient_id = maintainer.id
    initialize_database(engine)
    with factory() as db:
        assert db.get(Maintainer, recipient_id).display_name == "Existing"
        assert db.scalar(select(MaintainerClaim)) is None
    engine.dispose()
