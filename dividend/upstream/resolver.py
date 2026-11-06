"""Bounded injectable fallback resolver; provider responses are never trusted."""

import json
from typing import Protocol

from ..enrichment.base import FactSet
from .validator import Proposal, validate


class LLMProvider(Protocol):
    def structured_call(self, prompt: str, schema: dict, timeout: int = 30) -> object: ...


_SCHEMA = {"type": "object", "properties": {"proposals": {"type": "array", "items": {
    "type": "object", "properties": {
        "source_name": {"type": "string"}, "upstream_key": {"type": "string"},
        "display_name": {"type": "string"}, "evidence": {"type": "array", "items": {
            "type": "object", "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
            "required": ["field", "value"], "additionalProperties": False}},
    }, "required": ["source_name", "upstream_key", "display_name", "evidence"], "additionalProperties": False}},
}, "required": ["proposals"], "additionalProperties": False}


def _prompt(facts: list[FactSet]) -> str:
    # Only this explicit allowlist enters provider input; no document bodies exist here.
    rows = []
    for fact in facts:
        rows.append({key: value for key, value in {
            "source_name": fact.source_name,
            "udd.repository": fact.repository_url,
            "udd.homepage": fact.homepage,
            "udd.contact": fact.contact,
            "github.full_name": fact.github.full_name if fact.github else None,
            "github.owner_login": fact.github_owner_login,
            "github.parent_full_name": fact.github_parent_full_name,
        }.items() if value is not None})
    return "Propose upstream identities using only these structured facts. Return JSON.\n" + json.dumps(rows, sort_keys=True)


def _proposals(response: object) -> list[Proposal]:
    if type(response) is str:
        response = json.loads(response)
    rows = response.get("proposals") if type(response) is dict else None
    if type(rows) is not list:
        return []
    return [Proposal(**row) for row in rows if type(row) is dict and set(row) == {
        "source_name", "upstream_key", "display_name", "evidence"
    }]


def resolve(facts: list[FactSet], llm: LLMProvider, existing_keys: set[str] | None = None,
            max_attempts: int = 3) -> list[Proposal]:
    accepted: list[Proposal] = []
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    for start in range(0, len(facts), 10):
        pending = facts[start:start + 10]
        for _ in range(max_attempts):
            try:
                proposals = _proposals(llm.structured_call(_prompt(pending), _SCHEMA, timeout=30))
            except Exception:
                break
            by_name = {p.source_name: p for p in proposals if type(p) is Proposal}
            rejected = []
            for fact in pending:
                proposal = by_name.get(fact.source_name)
                if proposal and validate(proposal, fact, existing_keys):
                    accepted.append(proposal)
                else:
                    rejected.append(fact)
            pending = rejected
            if not pending:
                break
    return accepted
