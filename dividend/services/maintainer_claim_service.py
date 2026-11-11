"""Invitation-bound GitHub account claims for maintainers."""

from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..models import Maintainer, MaintainerClaim, MaintainerRecipient

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ClaimError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class MaintainerClaimService:
    def __init__(self, client_id: str | None, client_secret: str | None,
                 public_base_url: str, transport=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.public_base_url = public_base_url.rstrip("/")
        self.callback_url = f"{self.public_base_url}/api/maintainer-claims/oauth/callback"
        self.transport = transport

    @staticmethod
    def _claim_session(db: DbSession, session_hash: str | None) -> MaintainerClaim:
        claim = db.scalar(select(MaintainerClaim).where(
            MaintainerClaim.session_token_hash == session_hash,
            MaintainerClaim.session_expires_at > datetime.now(UTC),
        )) if session_hash else None
        if claim is None:
            raise ClaimError("maintainer_session_required", 401)
        return claim

    def issue_invitation(self, db: DbSession, recipient_id: str) -> str:
        recipient = db.get(Maintainer, recipient_id) or db.get(MaintainerRecipient, recipient_id)
        if recipient is None or recipient.opted_in is not True or recipient.active is not True:
            raise ClaimError("maintainer_not_eligible", 404)
        claim = db.scalar(select(MaintainerClaim).where(MaintainerClaim.recipient_id == recipient_id))
        if claim is not None and claim.status == "verified":
            raise ClaimError("maintainer_already_claimed")
        invitation = secrets.token_urlsafe(32)
        if claim is None:
            claim = MaintainerClaim(recipient_id=recipient.id,
                                    upstream_key=recipient.upstream_key)
            db.add(claim)
        claim.status = "invited"
        claim.invitation_token_hash = _hash(invitation)
        claim.invitation_expires_at = datetime.now(UTC) + timedelta(days=7)
        claim.session_token_hash = None
        claim.session_expires_at = None
        claim.oauth_state_hash = None
        claim.oauth_code_verifier = None
        claim.oauth_state_expires_at = None
        db.flush()
        return f"{self.public_base_url}/claim#token={invitation}"

    @staticmethod
    def redeem_invitation(db: DbSession, invitation: str) -> tuple[str, dict]:
        now = datetime.now(UTC)
        session_token = secrets.token_urlsafe(32)
        changed = db.execute(update(MaintainerClaim).where(
            MaintainerClaim.invitation_token_hash == _hash(invitation),
            MaintainerClaim.invitation_expires_at > now,
            MaintainerClaim.status == "invited",
        ).values(
            invitation_token_hash=None,
            invitation_expires_at=None,
            session_token_hash=_hash(session_token),
            session_expires_at=now + timedelta(hours=12),
            status="pending",
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise ClaimError("claim_invitation_invalid", 401)
        claim = db.scalar(select(MaintainerClaim).where(
            MaintainerClaim.session_token_hash == _hash(session_token)))
        return session_token, MaintainerClaimService._status(claim)

    @staticmethod
    def status(db: DbSession, session_hash: str | None) -> dict:
        claim = MaintainerClaimService._claim_session(db, session_hash)
        return MaintainerClaimService._status(claim)

    @staticmethod
    def _status(claim: MaintainerClaim) -> dict:
        return {"status": claim.status, "github_login": claim.github_login,
                "github_user_id": claim.github_user_id, "created_at": claim.created_at,
                "verified_at": claim.verified_at}

    def authorization_url(self, db: DbSession, session_hash: str | None) -> str:
        if not self.client_id or not self.client_secret:
            raise ClaimError("github_oauth_unavailable", 503)
        claim = self._claim_session(db, session_hash)
        if claim.status == "verified":
            raise ClaimError("maintainer_already_claimed")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        claim.oauth_state_hash = _hash(state)
        claim.oauth_code_verifier = verifier
        claim.oauth_state_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        query = urlencode({"client_id": self.client_id, "redirect_uri": self.callback_url,
                           "state": state, "code_challenge": challenge,
                           "code_challenge_method": "S256"})
        db.flush()
        return f"https://github.com/login/oauth/authorize?{query}"

    async def callback(self, db: DbSession, session_hash: str | None,
                       state: str | None, code: str | None, error: str | None) -> str:
        claim = self._claim_session(db, session_hash)
        if not state:
            raise ClaimError("oauth_state_invalid", 401)
        now = datetime.now(UTC)
        verifier = claim.oauth_code_verifier
        consumed = db.execute(update(MaintainerClaim).where(
            MaintainerClaim.id == claim.id,
            MaintainerClaim.session_token_hash == session_hash,
            MaintainerClaim.oauth_state_hash == _hash(state),
            MaintainerClaim.oauth_state_expires_at > now,
        ).values(
            oauth_state_hash=None,
            oauth_code_verifier=None,
            oauth_state_expires_at=None,
        ).execution_options(synchronize_session=False))
        if consumed.rowcount != 1:
            db.rollback()
            raise ClaimError("oauth_state_invalid", 401)
        db.commit()  # Consume state before contacting GitHub so it cannot be replayed.
        if error:
            return "denied" if error == "access_denied" else "authorization_failed"
        if not code or not verifier or not self.client_id or not self.client_secret:
            return "authorization_failed"

        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
                token_response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    headers={"Accept": "application/json"},
                    data={"client_id": self.client_id, "client_secret": self.client_secret,
                          "code": code, "redirect_uri": self.callback_url,
                          "code_verifier": verifier},
                )
                token_data = token_response.json()
                token = token_data.get("access_token") if token_response.is_success and isinstance(token_data, dict) else None
                if (not isinstance(token, str) or not token
                        or not isinstance(token_data, dict) or token_data.get("error")):
                    return "authorization_failed"
                if token_data.get("scope") not in (None, ""):
                    revoked = False
                    try:
                        revoke_response = await client.request(
                            "DELETE",
                            f"https://api.github.com/applications/{self.client_id}/token",
                            auth=(self.client_id, self.client_secret),
                            headers={"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"},
                            json={"access_token": token},
                        )
                        revoked = revoke_response.status_code in (204, 404)
                    except httpx.HTTPError:
                        pass
                    return "permissions_rejected" if revoked else "permissions_rejected_cleanup"
                user_response = await client.get(
                    "https://api.github.com/user",
                    headers={"Accept": "application/vnd.github+json",
                             "Authorization": f"Bearer {token}",
                             "X-GitHub-Api-Version": "2022-11-28"},
                )
                if user_response.status_code == 401:
                    return "authorization_revoked"
                if not user_response.is_success:
                    return "authorization_failed"
                user = user_response.json()
        except (httpx.HTTPError, ValueError):
            return "authorization_failed"

        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] <= 0 or not isinstance(user.get("login"), str)
                or not _LOGIN.fullmatch(user["login"])):
            return "authorization_failed"
        existing = db.scalar(select(MaintainerClaim).where(
            MaintainerClaim.github_user_id == user["id"]))
        if existing is not None and existing.id != claim.id:
            return "account_conflict"
        now = datetime.now(UTC)
        try:
            verified = db.execute(update(MaintainerClaim).where(
                MaintainerClaim.id == claim.id,
                MaintainerClaim.session_token_hash == session_hash,
                MaintainerClaim.session_expires_at > now,
                MaintainerClaim.status == "pending",
                MaintainerClaim.github_user_id.is_(None),
            ).values(
                github_user_id=user["id"],
                github_login=user["login"],
                status="verified",
                verified_at=now,
            ).execution_options(synchronize_session=False))
            if verified.rowcount != 1:
                db.rollback()
                return "authorization_failed"
            db.commit()
        except IntegrityError:
            db.rollback()
            return "account_conflict"
        return "verified"


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
