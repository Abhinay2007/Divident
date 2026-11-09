"""Bounded injectable fallback resolver; provider responses are never trusted."""

import hashlib
import json
import logging
from typing import Protocol

from ..enrichment.base import FactSet
from ..errors import RateLimitExceeded
from .validator import Proposal, validate

_logger = logging.getLogger(__name__)


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
_PROMPT_VERSION = "upstream-resolution-v1"


def _fact_values(fact: FactSet) -> dict:
    return {
        "source_name": fact.source_name,
        "udd.repository": fact.repository_url,
        "udd.homepage": fact.homepage,
        "github.full_name": fact.github.full_name if fact.github else None,
        "github.owner_login": fact.github_owner_login,
        "github.parent_full_name": fact.github_parent_full_name,
    }


def cache_key(fact: FactSet, model: str) -> str:
    payload = {"facts": _fact_values(fact), "model": model,
               "prompt_version": _PROMPT_VERSION, "schema": _SCHEMA}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _prompt(facts: list[FactSet]) -> str:
    # Only the evidence allowlist enters provider input; document bodies are excluded.
    rows = [{key: value for key, value in _fact_values(fact).items() if value is not None}
            for fact in facts]
    return "Propose upstream identities using only these structured facts. Return JSON.\n" + json.dumps(rows, sort_keys=True)


def _proposals(response: object) -> list[Proposal]:
    if type(response) is str:
        response = json.loads(response)
    rows = response.get("proposals") if type(response) is dict else None
    if type(rows) is not list:
        raise ValueError("Malformed structured proposal response")
    return [Proposal(**row) for row in rows if type(row) is dict and set(row) == {
        "source_name", "upstream_key", "display_name", "evidence"
    }]


def resolve(facts: list[FactSet], llm: LLMProvider, existing_keys: set[str] | None = None,
            max_attempts: int = 3, *, cache_get=None, cache_set=None,
            model: str = "unknown", cache_stats: dict | None = None) -> list[Proposal]:
    accepted: list[Proposal] = []
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    stats = cache_stats if cache_stats is not None else {}
    stats.update(cache_hits=0, cache_misses=0, provider_failures=0, limit_rejections=0, llm_calls=0)
    stats["cached_sources"] = []
    pending_facts = []
    for fact in facts:
        cached = cache_get(fact, model) if cache_get is not None else None
        try:
            rows = _proposals(cached) if cached is not None else []
            proposal = next((item for item in rows if item.source_name == fact.source_name), None)
        except (TypeError, ValueError, json.JSONDecodeError):
            proposal = None
        if proposal is not None and validate(proposal, fact, existing_keys):
            accepted.append(proposal)
            stats["cache_hits"] += 1
            stats["cached_sources"].append(fact.source_name)
        else:
            if cache_get is not None:
                stats["cache_misses"] += 1
            pending_facts.append(fact)

    for start in range(0, len(pending_facts), 10):
        pending = pending_facts[start:start + 10]
        for _ in range(max_attempts):
            try:
                stats["llm_calls"] += 1
                proposals = _proposals(llm.structured_call(_prompt(pending), _SCHEMA, timeout=30))
            except Exception as exc:
                if isinstance(exc, RateLimitExceeded):
                    stats["limit_rejections"] += 1
                    _logger.info("Live LLM call limit reached; unresolved candidates remain unmapped")
                else:
                    stats["provider_failures"] += 1
                    _logger.warning("Upstream LLM call failed; unresolved candidates remain unmapped")
                break
            by_name = {p.source_name: p for p in proposals if type(p) is Proposal}
            rejected = []
            for fact in pending:
                proposal = by_name.get(fact.source_name)
                if proposal and validate(proposal, fact, existing_keys):
                    accepted.append(proposal)
                    if cache_set is not None:
                        cache_set(fact, model, {"proposals": [{
                                "source_name": proposal.source_name,
                                "upstream_key": proposal.upstream_key,
                                "display_name": proposal.display_name,
                                "evidence": proposal.evidence,
                            }]})
                else:
                    rejected.append(fact)
            pending = rejected
            if not pending:
                break
    return accepted
