"""Bounded, read-only Modal summaries of frozen proposal evidence."""

import hashlib
import json
import logging
import re
from threading import Lock
from urllib.parse import urlsplit

from ..errors import LLMProviderError, RateLimitExceeded
from ..models import LLMCache
from ..llm.modal_provider import count_input_tokens
from .plan_service import PlanService

PROMPT_VERSION = "maintainer-evidence-review-v7"
INPUT_TOKEN_BUDGET = 2500
MIN_SUMMARY_NON_WHITESPACE_CHARS = 1
MAX_SUMMARY_CHARS = 1500
_REQUEST_LOCK = Lock()
_logger = logging.getLogger(__name__)
_PR_CITATION = re.compile(r"(?:#|\bPR\s*#?)\s*(\d+)", re.IGNORECASE)
_UNSUPPORTED_FILE_CLAIM = re.compile(
    r"\b(?:file[- ]level|docs?[- ]only|documentation[- ]only|documentation contributions?)\b",
    re.IGNORECASE,
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS},
        "contributors": {"type": "array", "items": {"type": "object",
            "properties": {
                "contributor_id": {"type": "string"},
                "observation": {"type": "string"},
                "cited_pr_numbers": {"type": "array", "items": {"type": "integer"}},
            }, "required": ["contributor_id", "observation", "cited_pr_numbers"],
            "additionalProperties": False}},
        "cited_pr_numbers": {"type": "array", "items": {"type": "integer"}},
        "advisory_flags": {"type": "array", "items": {"type": "string",
            "enum": ["merged_pr_activity", "review_activity", "single_pr_evidence",
                     "multiple_pr_evidence"]}},
    }, "required": ["summary", "contributors", "cited_pr_numbers", "advisory_flags"],
    "additionalProperties": False,
}


class EvidenceValidationError(Exception):
    code = "llm_schema_violation"

    def __init__(self, field: str, reason: str):
        super().__init__("Evidence review output failed validation")
        self.detail = {"field": field, "reason": reason}


class EvidenceReviewService:
    def __init__(self, session_factory, provider=None,
                 model: str = "deepseek-ai/DeepSeek-V4.1-Flash-1017"):
        self.session_factory = session_factory
        self.provider = provider
        self.model = model

    def review(self, plan_id: str, db) -> dict:
        plan = PlanService.get(db, plan_id)
        try:
            PlanService.verify_approval_proposal(db, plan)
        except Exception:
            return self._unavailable("proposal_invalid", None)
        frozen = plan.fee_policy_snapshot["approval_proposal"]
        proposal, proposal_hash = frozen["snapshot"], frozen["sha256"]
        cache_key = hashlib.sha256(
            f"{proposal_hash}:{self.model}:{PROMPT_VERSION}".encode("utf-8")
        ).hexdigest()
        cached = self._cache(cache_key)
        if cached is not None:
            _logger.debug("Evidence review cache hit provider=%s model=%s",
                          self._provider_name(), self.model)
            return cached
        _logger.debug("Evidence review cache miss provider=%s model=%s",
                      self._provider_name(), self.model)
        if self.provider is None:
            return self._unavailable("modal_disabled", proposal_hash)

        contributors = self._aggregate(proposal)
        if not contributors:
            return self._unavailable("no_validated_evidence", proposal_hash)
        evidence = [{
            "id": row["contributor_id"],
            "merged": [{"pr": item["pr_number"], "url": item["url"]}
                       for item in row["evidence_references"]
                       if item["signal"] == "merged_pull_request"],
            "reviewed": [{"pr": item["pr_number"], "url": item["url"]}
                         for item in row["evidence_references"]
                         if item["signal"] == "code_review"],
        } for row in contributors.values()]
        all_pr_numbers = {
            number for contributor in contributors.values()
            for number in contributor["merged_pr_numbers"] + contributor["reviewed_pr_numbers"]
        }
        supported_flags = set()
        if any(row["merged_pr_numbers"] for row in contributors.values()):
            supported_flags.add("merged_pr_activity")
        if any(row["reviewed_pr_numbers"] for row in contributors.values()):
            supported_flags.add("review_activity")
        supported_flags.add("single_pr_evidence" if len(all_pr_numbers) == 1
                            else "multiple_pr_evidence")
        prompt = (
            "Write a concise, donor-facing open-source contribution review in professional, "
            "natural English. Treat the supplied records as the complete evidence; do not "
            "infer names, identities, code quality, project impact, maintainer status, or "
            "current involvement.\n"
            "summary: write 2–4 concise sentences as an executive summary, with no generic "
            "opening. Target approximately 700–1100 characters including spaces and never "
            "exceed 1500 characters. Describe the overall picture and distinguish merged contributions "
            "from code reviews. Avoid repeating details from contributor highlights. Qualify "
            "conclusions with 'in the supplied evidence' where appropriate.\n"
            "contributors: return one highlight for every supplied ID, preserving each ID "
            "exactly. Use varied sentence structures to interpret the activity pattern rather "
            "than repeat a count formula; exact distinct merged-PR and reviewed-PR counts are "
            "shown separately by the application. The input arrays contain distinct PRs, so "
            "multiple review events on one PR count once. Do not imply review-only activity "
            "includes a merge. Use neutral activity groupings only when supported. Never invent "
            "names. Do not print PR lists in prose; use cited_pr_numbers for references.\n"
            "cited_pr_numbers must equal exactly the union of the contributors' cited_pr_numbers "
            "and any PR references explicitly written in summary. Do not add uncited evidence PRs.\n"
            "Return exactly one JSON object matching the supplied schema, no markdown or "
            "surrounding text. advisory_flags must be selected only from this "
            "evidence-supported set: " + json.dumps(sorted(supported_flags), separators=(",", ":"))
            + "\n" + json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
        input_tokens = count_input_tokens(prompt, _SCHEMA)
        if input_tokens > INPUT_TOKEN_BUDGET:
            _logger.warning(
                "Evidence review rejected error_type=InputTokenBudgetExceeded provider=%s "
                "model=%s input_tokens=%d token_budget=%d",
                self._provider_name(), self.model, input_tokens, INPUT_TOKEN_BUDGET,
            )
            return self._unavailable("input_token_budget_exceeded", proposal_hash)

        # One process-wide lock serializes Modal work and makes duplicate misses coalesce.
        with _REQUEST_LOCK:
            cached = self._cache(cache_key)
            if cached is not None:
                _logger.debug("Evidence review cache hit provider=%s model=%s",
                              self._provider_name(), self.model)
                return cached
            try:
                raw = self.provider.structured_call(prompt, _SCHEMA, timeout=30)
                result = self._validate(raw, contributors, proposal_hash, self.model)
            except Exception as exc:
                _logger.warning(
                    "Evidence review failed error_type=%s error_code=%s provider=%s "
                    "model=%s input_tokens=%d validation_field=%s validation_reason=%s "
                    "summary_type=%s summary_chars=%s expected_summary_min_non_whitespace_chars=%s "
                    "expected_summary_max_chars=%s summary_constraint=%s validation_details=%s",
                    type(exc).__name__, getattr(exc, "code", "unknown"),
                    self._provider_name(), self.model, input_tokens,
                    (getattr(exc, "detail", {}) or {}).get("field", "provider"),
                    (getattr(exc, "detail", {}) or {}).get("reason", "request_failed"),
                    (getattr(exc, "detail", {}) or {}).get("summary_type", "n/a"),
                    (getattr(exc, "detail", {}) or {}).get("summary_chars", "n/a"),
                    MIN_SUMMARY_NON_WHITESPACE_CHARS,
                    MAX_SUMMARY_CHARS,
                    (getattr(exc, "detail", {}) or {}).get("summary_constraint", "n/a"),
                    {key: value for key, value in (getattr(exc, "detail", {}) or {}).items()
                     if key in {"citation_count", "invalid_citation_count", "attribution_mismatch",
                                "flag_count", "unsupported_flag_count", "expected_citation_count",
                                "missing_citation_count", "extra_citation_count"}},
                )
                if getattr(exc, "code", None) == "llm_schema_violation":
                    return {"status": "invalid", "reason": "invalid_model_output",
                            "proposal_sha256": proposal_hash, "model": self.model,
                            "prompt_version": PROMPT_VERSION}
                if isinstance(exc, RateLimitExceeded):
                    return self._unavailable(exc.code, proposal_hash,
                                             retry_after_seconds=exc.retry_after_seconds)
                if isinstance(exc, LLMProviderError):
                    return self._unavailable(exc.code, proposal_hash)
                return self._unavailable("modal_endpoint_unavailable", proposal_hash)
            if result is None:
                return {"status": "invalid", "reason": "invalid_model_output",
                        "proposal_sha256": proposal_hash, "model": self.model,
                        "prompt_version": PROMPT_VERSION}
            with self.session_factory() as store:
                store.add(LLMCache(key=cache_key, response=result))
                store.commit()
            return result

    def _provider_name(self) -> str:
        return type(self.provider).__name__ if self.provider is not None else "disabled"

    @staticmethod
    def _aggregate(proposal: dict) -> dict[str, dict]:
        output = {}
        for project in proposal.get("projects", []):
            for row in project.get("evidence", []):
                user_id = row.get("github_user_id")
                pr_number, signal = row.get("pr_number"), row.get("signal_type")
                reference = row.get("source_url")
                if (type(user_id) is not int or user_id <= 0 or type(pr_number) is not int
                        or pr_number <= 0
                        or type(signal) is not str
                        or signal not in {"merged_pull_request", "code_review"}
                        or type(reference) is not str):
                    continue
                contributor_id = str(user_id)
                item = output.setdefault(contributor_id, {
                    "contributor_id": contributor_id, "merged_pr_numbers": set(),
                    "reviewed_pr_numbers": set(), "evidence_references": set(),
                })
                field = "merged_pr_numbers" if signal == "merged_pull_request" else "reviewed_pr_numbers"
                item[field].add(pr_number)
                item["evidence_references"].add((signal, pr_number, reference))
        return {key: {
            "contributor_id": item["contributor_id"],
            "merged_pr_numbers": sorted(item["merged_pr_numbers"]),
            "reviewed_pr_numbers": sorted(item["reviewed_pr_numbers"]),
            "evidence_references": [{"signal": signal, "pr_number": number, "url": url}
                                    for signal, number, url in sorted(item["evidence_references"])],
        } for key, item in sorted(output.items())}

    @classmethod
    def _validate(cls, raw: dict, contributors: dict[str, dict], proposal_hash: str,
                  model: str = "deepseek-ai/DeepSeek-V4.1-Flash-1017"):
        def invalid(field: str, reason: str, **diagnostics):
            error = EvidenceValidationError(field, reason)
            error.detail.update(diagnostics)
            raise error

        if type(raw) is not dict or set(raw) != {
            "summary", "contributors", "cited_pr_numbers", "advisory_flags",
        }:
            invalid("$", "unexpected_or_missing_fields")
        allowed_by_contributor = {
            key: set(row["merged_pr_numbers"] + row["reviewed_pr_numbers"])
            for key, row in contributors.items()
        }
        all_prs = set().union(*allowed_by_contributor.values())
        supported_flags = set()
        merged = any(row["merged_pr_numbers"] for row in contributors.values())
        reviewed = any(row["reviewed_pr_numbers"] for row in contributors.values())
        if merged:
            supported_flags.add("merged_pr_activity")
        if reviewed:
            supported_flags.add("review_activity")
        if len(all_prs) == 1:
            supported_flags.add("single_pr_evidence")
        elif len(all_prs) > 1:
            supported_flags.add("multiple_pr_evidence")
        summary = raw["summary"]
        summary_type = type(summary).__name__
        summary_chars = len(summary) if type(summary) is str else None
        if type(summary) is not str:
            invalid("summary", "invalid_text_or_length", summary_type=summary_type,
                    summary_chars=summary_chars, summary_constraint="expected_string")
        if len(summary.strip()) < MIN_SUMMARY_NON_WHITESPACE_CHARS:
            invalid("summary", "invalid_text_or_length", summary_type=summary_type,
                    summary_chars=summary_chars, summary_constraint="empty_or_whitespace")
        if len(summary) > MAX_SUMMARY_CHARS:
            invalid("summary", "invalid_text_or_length", summary_type=summary_type,
                    summary_chars=summary_chars, summary_constraint="exceeds_max_length")
        if type(raw["contributors"]) is not list:
            invalid("contributors", "expected_array")
        if type(raw["cited_pr_numbers"]) is not list:
            invalid("cited_pr_numbers", "expected_array")
        if type(raw["advisory_flags"]) is not list:
            invalid("advisory_flags", "expected_array")
        flags = raw["advisory_flags"]
        if any(type(flag) is not str for flag in flags):
            invalid("advisory_flags", "invalid_item_type")
        if len(flags) != len(set(flags)):
            invalid("advisory_flags", "duplicate_flag")
        if set(flags) - supported_flags:
            invalid("advisory_flags", "unsupported_flag", flag_count=len(flags),
                    unsupported_flag_count=len(set(flags) - supported_flags))
        summary_citations = cls._text_citations(raw["summary"])
        if summary_citations - all_prs:
            invalid("summary", "citation_not_in_evidence", citation_count=len(summary_citations),
                    invalid_citation_count=len(summary_citations - all_prs))
        if _UNSUPPORTED_FILE_CLAIM.search(raw["summary"]):
            invalid("summary", "unsupported_file_level_claim")
        seen, validated = set(), []
        for item in raw["contributors"]:
            if (type(item) is not dict or set(item) != {
                "contributor_id", "observation", "cited_pr_numbers",
            }):
                invalid("contributors[]", "unexpected_or_missing_fields")
            identity, observation, citations = (item["contributor_id"], item["observation"],
                                                 item["cited_pr_numbers"])
            if type(identity) is not str or identity not in allowed_by_contributor:
                invalid("contributors[].contributor_id", "unknown_contributor_id")
            if identity in seen:
                invalid("contributors[].contributor_id", "duplicate_contributor_id")
            if (type(observation) is not str or not observation.strip()
                    or len(observation) > 300):
                invalid("contributors[].observation", "invalid_text_or_length")
            if _UNSUPPORTED_FILE_CLAIM.search(observation):
                invalid("contributors[].observation", "unsupported_file_level_claim")
            if type(citations) is not list:
                invalid("contributors[].cited_pr_numbers", "expected_array")
            if any(type(number) is not int for number in citations):
                invalid("contributors[].cited_pr_numbers", "invalid_item_type")
            if len(citations) != len(set(citations)):
                invalid("contributors[].cited_pr_numbers", "duplicate_citation")
            invalid_citations = set(citations) - allowed_by_contributor[identity]
            if invalid_citations:
                invalid("contributors[].cited_pr_numbers", "citation_not_in_evidence",
                        citation_count=len(citations), invalid_citation_count=len(invalid_citations),
                        attribution_mismatch=bool(invalid_citations & all_prs))
            observation_citations = cls._text_citations(observation)
            invalid_observation_citations = observation_citations - allowed_by_contributor[identity]
            if invalid_observation_citations:
                invalid("contributors[].observation", "citation_not_in_evidence",
                        citation_count=len(observation_citations),
                        invalid_citation_count=len(invalid_observation_citations),
                        attribution_mismatch=bool(invalid_observation_citations & all_prs))
            if cls._text_citations(observation) - set(citations):
                invalid("contributors[].observation", "citation_missing_from_cited_pr_numbers")
            seen.add(identity)
            validated.append({"contributor_id": identity, "observation": observation,
                              "cited_pr_numbers": sorted(citations),
                              "merged_pr_count": len(contributors[identity]["merged_pr_numbers"]),
                              "reviewed_pr_count": len(contributors[identity]["reviewed_pr_numbers"])})
        cited = raw["cited_pr_numbers"]
        expected_citations = set().union(*(set(item["cited_pr_numbers"]) for item in validated))
        expected_citations.update(cls._text_citations(raw["summary"]))
        if seen != set(contributors):
            invalid("contributors", "missing_contributor")
        if any(type(number) is not int for number in cited):
            invalid("cited_pr_numbers", "invalid_item_type")
        if len(cited) != len(set(cited)):
            invalid("cited_pr_numbers", "duplicate_citation")
        invalid_citations = set(cited) - all_prs
        if invalid_citations:
            invalid("cited_pr_numbers", "citation_not_in_evidence", citation_count=len(cited),
                    invalid_citation_count=len(invalid_citations))
        if set(cited) != expected_citations:
            missing_citations = expected_citations - set(cited)
            extra_citations = set(cited) - expected_citations
            invalid("cited_pr_numbers", "citations_do_not_match_findings",
                    citation_count=len(cited), expected_citation_count=len(expected_citations),
                    missing_citation_count=len(missing_citations),
                    extra_citation_count=len(extra_citations))
        citation_urls = set()
        citation_details = set()
        for contributor_id, contributor in contributors.items():
            for evidence in contributor["evidence_references"]:
                number, reference = evidence["pr_number"], evidence["url"]
                if number not in cited:
                    continue
                try:
                    parsed = urlsplit(reference)
                except ValueError:
                    continue
                match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:/.*)?",
                                     parsed.path)
                if (parsed.scheme == "https" and parsed.hostname == "github.com" and match
                        and int(match.group(3)) == number):
                    url = f"https://github.com/{match.group(1)}/{match.group(2)}/pull/{number}"
                    citation_urls.add((number, url))
                    citation_details.add((contributor_id, evidence["signal"], number, url))
        if {number for number, _ in citation_urls} != set(cited):
            invalid("citations", "missing_verified_github_pr_url")
        return {"status": "available", "summary": raw["summary"],
                "contributors": sorted(validated, key=lambda row: row["contributor_id"]),
                "cited_pr_numbers": sorted(cited),
                "citations": [{"contributor_id": contributor_id, "activity_type": activity_type,
                               "pr_number": number, "url": url}
                              for contributor_id, activity_type, number, url
                              in sorted(citation_details)],
                "advisory_flags": sorted(raw["advisory_flags"]),
                "proposal_sha256": proposal_hash, "model": model,
                "prompt_version": PROMPT_VERSION}

    @staticmethod
    def _text_citations(value: str) -> set[int]:
        return {int(number) for number in _PR_CITATION.findall(value)}

    def _cache(self, key: str):
        with self.session_factory() as store:
            row = store.get(LLMCache, key)
            return row.response if row is not None and row.response.get("status") == "available" else None

    @staticmethod
    def _unavailable(reason: str, proposal_hash: str | None, **extra) -> dict:
        return {"status": "unavailable", "reason": reason,
                "proposal_sha256": proposal_hash, **extra}
