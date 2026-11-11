"""Canonical approval proposal hashing and integrity verification."""

import hashlib
import json


def canonical_proposal_bytes(proposal: dict) -> bytes:
    return json.dumps(proposal, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def proposal_sha256(proposal: dict) -> str:
    return hashlib.sha256(canonical_proposal_bytes(proposal)).hexdigest()


def freeze_proposal(proposal: dict) -> dict:
    return {"snapshot": proposal, "sha256": proposal_sha256(proposal)}
