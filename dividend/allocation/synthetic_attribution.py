"""Deterministic attribution used only by the synthetic Sandbox demo lane."""

from pathlib import Path

import yaml


DATA = Path(__file__).resolve().parents[1] / "data"
FIXTURE_PATH = DATA / "synthetic_sandbox_demo.yaml"
POLICY_PATH = DATA / "synthetic_demo_policy.yaml"
SIGNALS = {"merged_pull_request", "accepted_commit", "code_review", "resolved_issue"}


def load_fixture() -> tuple[dict, dict]:
    fixture = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    if (type(fixture) is not dict or fixture.get("schema") != 1
            or fixture.get("fixture_version") != "synthetic-curl-contributors-v1"
            or fixture.get("project_key") != "github.com/curl/curl"
            or type(fixture.get("contributors")) is not list):
        raise ValueError("invalid synthetic contributor fixture")
    if (type(policy) is not dict or policy.get("schema") != 1
            or policy.get("policy_version") != "synthetic-attribution-single-sandbox-destination-v1"
            or policy.get("method") != "supported-fictional-evidence-enables-one-configured-sandbox-destination"
            or policy.get("required_distinct_signal_types") != 2):
        raise ValueError("invalid synthetic attribution policy")
    for row in fixture["contributors"]:
        if type(row) is not dict:
            raise ValueError("invalid synthetic contributor")
        evidence = row.get("evidence")
        if (type(row.get("id")) is not str or not row["id"]
                or type(row.get("label")) is not str or not row["label"].startswith("Fictional ")
                or type(evidence) is not list):
            raise ValueError("invalid synthetic contributor")
        signal_types = set()
        for item in evidence:
            if (type(item) is not dict or item.get("signal_type") not in SIGNALS
                    or type(item.get("reference")) is not str
                    or not item["reference"].startswith("synthetic://")):
                raise ValueError("synthetic evidence must use supported signals and synthetic:// references")
            signal_types.add(item["signal_type"])
        if len(signal_types) < policy["required_distinct_signal_types"]:
            raise ValueError("each fictional contributor needs at least two distinct signal types")
    return fixture, policy


def attribute(amount_cents: int, recipient_id: str, display_name: str,
              fixture: dict, policy: dict) -> tuple[list[dict], list[dict]]:
    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValueError("synthetic payable amount must be positive integer cents")
    evidence = [{
        "contributor_id": contributor["id"],
        "label": contributor["label"],
        "signal_type": item["signal_type"],
        "reference": item["reference"],
        "verified": False,
    } for contributor in fixture["contributors"] for item in contributor["evidence"]]
    if not evidence:
        raise ValueError("synthetic fixture has no supporting evidence")
    return ([{"maintainer_id": recipient_id, "display_name": display_name,
              "share_bps": 10000, "amount_cents": amount_cents}], evidence)
