"""Deterministic maintainer distribution from verified contribution evidence."""

from collections import defaultdict

ALGORITHM_VERSION = "maintainer-evidence-v1"
SERVER_EVIDENCE_PROVENANCE = "server-validated-github-snapshot-v1"
SIGNAL_POINTS = {
    "merged_pull_request": 3,
    "accepted_commit": 2,
    "code_review": 1,
    "resolved_issue": 2,
    "release_contribution": 2,
    "security_maintenance": 2,
}


def distribute(amount_cents: int, maintainers: list[dict], evidence: list[dict]) -> dict:
    """Return deterministic proposed cents and reserve any unsupported remainder.

    Evidence rows must be verified, identify an opted-in maintainer, and cite a
    stable external reference. Multiple evidence types are required per person.
    """
    if type(amount_cents) is not int or amount_cents < 0:
        raise ValueError("amount_cents must be nonnegative integer cents")
    eligible = {m["id"]: m for m in maintainers if m.get("opted_in") is True}
    points, refs, signals = defaultdict(int), defaultdict(list), defaultdict(set)
    unsupported_points = 0
    for row in evidence:
        if type(row) is not dict:
            continue
        signal, mid, ref = row.get("signal_type"), row.get("maintainer_id"), row.get("reference")
        if (row.get("provenance") != SERVER_EVIDENCE_PROVENANCE or signal not in SIGNAL_POINTS
                or type(ref) is not str or not ref.startswith("https://")):
            continue
        if mid not in eligible:
            unsupported_points += SIGNAL_POINTS[signal]
            continue
        points[mid] += SIGNAL_POINTS[signal]
        refs[mid].append(ref)
        signals[mid].add(signal)
    scores = {}
    for mid, score in points.items():
        if len(signals[mid]) >= 2:
            scores[mid] = score
        else:
            unsupported_points += score
    supported_score = sum(scores.values())
    total_score = supported_score + unsupported_points
    if not total_score or not amount_cents or not scores:
        return {"algorithm_version": ALGORITHM_VERSION, "maintainers": [],
                "reserved_cents": amount_cents,
                "reserved_reason": "No sufficient verified maintainer contribution evidence."}
    if amount_cents < len(scores):
        return {"algorithm_version": ALGORITHM_VERSION, "maintainers": [],
                "reserved_cents": amount_cents,
                "reserved_reason": "Insufficient cents to pay every supported maintainer."}
    floors, remainders = {}, {}
    shares, share_remainders = {}, {}
    for mid, score in scores.items():
        floors[mid], remainders[mid] = divmod(amount_cents * score, total_score)
    reserved_cents, reserved_remainder = divmod(amount_cents * unsupported_points, total_score)
    remaining = amount_cents - reserved_cents - sum(floors.values())
    remainders["__reserved__"] = reserved_remainder
    for key in sorted(remainders, key=lambda value: (-remainders[value], value))[:remaining]:
        if key == "__reserved__":
            reserved_cents += 1
        else:
            floors[key] += 1
    payable_maintainers = [mid for mid in scores if floors[mid] > 0]
    shares = {mid: 0 for mid in payable_maintainers}
    share_remainders = {}
    payable_score = sum(scores[mid] for mid in payable_maintainers)
    for mid in payable_maintainers:
        shares[mid], share_remainders[mid] = divmod(10000 * scores[mid], payable_score)
    for mid in sorted(share_remainders, key=lambda value: (-share_remainders[value], value))[:10000 - sum(shares.values())]:
        shares[mid] += 1
    recipients = []
    for mid in sorted(payable_maintainers):
        recipients.append({"maintainer_id": mid, "display_name": eligible[mid]["display_name"],
                           "score": scores[mid], "share_bps": shares[mid],
                           "amount_cents": floors[mid], "evidence_references": sorted(set(refs[mid]))})
    return {"algorithm_version": ALGORITHM_VERSION, "maintainers": recipients,
            "reserved_cents": reserved_cents,
            "reserved_reason": ("Some contribution evidence was unmapped or insufficient."
                                if reserved_cents else None)}
