"""Evidence-backed explanations over immutable plan snapshots."""

import json

from sqlalchemy.orm import Session as DbSession

from ..allocation.snapshot import facts_snapshot_from_dict
from ..errors import NotFoundError
from ..models import Session as SessionRecord
from .plan_service import PlanService


_SCHEMA = lambda ids: {"type": "object", "properties": {
    "claim_ids": {"type": "array", "items": {"type": "string", "enum": ids},
                     "uniqueItems": True},
}, "required": ["claim_ids"], "additionalProperties": False}


class ExplanationService:
    """Groq may order evidence claims; only server-authored claims reach users."""

    @staticmethod
    def get_plan_explanations(db: DbSession, plan_id: str, llm=None) -> dict:
        plan = PlanService.get(db, plan_id)
        session = db.get(SessionRecord, plan.session_id)
        if session is None:
            raise NotFoundError("Session not found", code="session_not_found")
        explanations = []
        for line in plan.lines:
            claims, evidence = ExplanationService._claims(line, session.source_binaries or {})
            ordered_ids: list[str] = []
            status = "disabled"
            if llm is not None:
                prompt = "Select and order claim IDs that best explain this frozen allocation. " \
                         "Return only IDs from the supplied claims. Do not add or rewrite claims.\n" \
                         + json.dumps(claims, sort_keys=True, separators=(",", ":"))
                try:
                    result = llm.structured_call(prompt, _SCHEMA([item["id"] for item in claims]), timeout=20)
                    selected = result.get("claim_ids") if type(result) is dict else None
                    if (type(selected) is list and len(selected) == len(set(selected))
                            and all(value in {item["id"] for item in claims} for value in selected)):
                        ordered_ids = selected
                        status = "groq_ordered"
                    else:
                        status = "invalid_response_fallback"
                except Exception:
                    status = "unavailable_fallback"
            by_id = {item["id"]: item for item in claims}
            order = ordered_ids + [item["id"] for item in claims if item["id"] not in ordered_ids]
            explanations.append({
                "upstream_key": line.upstream_key,
                "status": status,
                "claims": [{**by_id[claim_id], "text": by_id[claim_id]["text"]}
                           for claim_id in order],
                "evidence": evidence,
            })
        return {"plan_id": plan.id, "explanations": explanations}

    @staticmethod
    def _claims(line, source_binaries: dict) -> tuple[list[dict], list[dict]]:
        snapshot = facts_snapshot_from_dict(line.facts_snapshot)
        facts = snapshot.normalized
        evidence: list[dict] = []
        claims: list[dict] = []

        def add(claim_id: str, text: str, refs: list[str]):
            claims.append({"id": claim_id, "text": text, "evidence_ids": refs})

        add("purpose", "Project description: unavailable in the structured project record.",
            ["MISSING:project-description"])
        package_refs = []
        relationships = []
        for source in snapshot.sources:
            binaries = sorted(source_binaries.get(source.source_name, []))
            ref = f"PACKAGE:{source.source_name}"
            package_refs.append(ref)
            evidence.append({"id": ref, "source_package": source.source_name,
                             "binary_packages": binaries, "source": "session scan"})
            relationships.append(f"{source.source_name} provides {', '.join(binaries)}" if binaries
                                 else f"{source.source_name} has no binary membership in this scan")
        add("packages", "Debian inventory relationship: " + ("; ".join(relationships) or "no source packages recorded") + ".",
            package_refs or ["MISSING:package-relationships"])
        add("relevance", f"The project is relevant to this inventory through {len(snapshot.sources)} verified source package(s) and {facts.binary_count} binary package(s).",
            package_refs or ["MISSING:package-relationships"])

        identity_refs = []
        identity_values = []
        for source in snapshot.sources:
            resolution = source.resolution
            if resolution.validated:
                for item in resolution.evidence or ():
                    ref = f"IDENTITY:{source.source_name}:{item.field}"
                    identity_refs.append(ref)
                    identity_values.append(f"{item.field}={item.value}")
                    evidence.append({"id": ref, "source_package": source.source_name,
                                     "field": item.field, "value": item.value,
                                     "method": resolution.method, "validated": True})
        add("identity", "Verified upstream identity evidence: " + ("; ".join(identity_values) if identity_values else "no qualifying evidence recorded") + ".",
            identity_refs or ["MISSING:identity-evidence"])

        activity_refs = []
        activity = []
        for field, value in (("committers_12m", facts.committers_12m), ("last_commit", facts.last_commit)):
            if value is not None:
                ref = f"ACTIVITY:{line.upstream_key}:{field}"
                activity_refs.append(ref)
                activity.append(f"{field}={value}")
                evidence.append({"id": ref, "field": field, "value": value,
                                 "source": "structured GitHub metadata"})
        add("activity", "Contribution activity: " + (", ".join(activity) if activity else "cannot be established from available metadata") + ".",
            activity_refs or ["MISSING:contribution-activity"])

        eligible = line.payable_cents > 0
        eligibility_ref = f"ELIGIBILITY:{line.upstream_key}"
        evidence.append({"id": eligibility_ref, "opted_in_maintainer_available": eligible,
                         "payable_cents": line.payable_cents, "reserved_cents": line.reserved_cents,
                         "source": "frozen plan eligibility result"})
        add("maintainer", ("An opted-in maintainer was eligible at plan creation." if eligible
                            else "No opted-in maintainer was available at plan creation; the allocation is reserved."),
            [eligibility_ref])

        allocation_ref = f"ALLOCATION:{line.upstream_key}"
        evidence.append({"id": allocation_ref, "amount_cents": line.amount_cents,
                         "payable_cents": line.payable_cents, "reserved_cents": line.reserved_cents,
                         "reason": line.reason, "binary_count": facts.binary_count,
                         "archived": facts.archived, "last_commit": facts.last_commit,
                         "committers_12m": facts.committers_12m, "partial": facts.partial})
        add("allocation", f"The deterministic allocation assigned ${line.amount_cents / 100:.2f}: {line.reason}",
            [allocation_ref])
        add("split", f"${line.payable_cents / 100:.2f} is payable and ${line.reserved_cents / 100:.2f} is reserved.",
            [eligibility_ref, allocation_ref])
        limitations = []
        if facts.partial:
            limitations.append("enrichment was partial")
        if facts.committers_12m is None:
            limitations.append("contributor count is missing")
        if facts.last_commit is None:
            limitations.append("last commit date is missing")
        limitations.append("project description is not stored")
        confidence = "High identity confidence from validated mapping; funding rationale is limited by " + ", ".join(limitations) + "."
        limitation_ref = f"LIMITATIONS:{line.upstream_key}"
        evidence.append({"id": limitation_ref, "partial": facts.partial,
                         "committers_12m_available": facts.committers_12m is not None,
                         "last_commit_available": facts.last_commit is not None,
                         "description_available": False})
        add("confidence", confidence, [limitation_ref])
        return claims, evidence
