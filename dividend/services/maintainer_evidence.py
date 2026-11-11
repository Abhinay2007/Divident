"""Server-side loading and validation of recorded maintainer evidence."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = _ROOT / "data" / "curl_maintainer_evidence.snapshot.json"
IDENTITY_MAP_PATH = _ROOT / "data" / "demo_github_identity_map.yaml"
_REPOSITORY = "github.com/curl/curl"
_SIGNALS = {"merged_pull_request", "code_review"}


def load_demo_identity_map(path: Path = IDENTITY_MAP_PATH) -> dict[int, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (type(raw) is not dict or raw.get("schema") != 1
            or raw.get("mapping_label") != "SYNTHETIC DEMO MAPPING ONLY; this is not a verified identity claim"
            or raw.get("repository_key") != _REPOSITORY or type(raw.get("identities")) is not list):
        raise ValueError("invalid synthetic GitHub identity mapping")
    mapping = {}
    for row in raw["identities"]:
        if (type(row) is not dict or set(row) != {"github_user_id", "maintainer_upstream_key"}
                or type(row.get("github_user_id")) is not int
                or row["github_user_id"] <= 0 or row.get("maintainer_upstream_key") != _REPOSITORY
                or row["github_user_id"] in mapping):
            raise ValueError("invalid synthetic GitHub identity mapping row")
        mapping[row["github_user_id"]] = row["maintainer_upstream_key"]
    return mapping


def load_recorded_evidence(path: Path = SNAPSHOT_PATH) -> list[dict]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    repository = snapshot.get("repository") if type(snapshot) is dict else None
    if (type(snapshot) is not dict
            or snapshot.get("snapshot_label") != "recorded GitHub maintainer evidence snapshot"
            or type(repository) is not dict or repository.get("key") != _REPOSITORY
            or type(snapshot.get("evidence")) is not list):
        raise ValueError("invalid recorded curl maintainer evidence snapshot")
    try:
        start = datetime.fromisoformat(snapshot["lookback_start"].replace("Z", "+00:00")).astimezone(UTC)
        end = datetime.fromisoformat(snapshot["lookback_end"].replace("Z", "+00:00")).astimezone(UTC)
        captured = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid recorded snapshot timestamps") from exc
    if end != captured or start != end - timedelta(days=365):
        raise ValueError("recorded snapshot lookback does not match its capture time")

    records, event_ids = [], set()
    for row in snapshot["evidence"]:
        if type(row) is not dict:
            continue
        event_id, user_id = row.get("event_id"), row.get("github_user_id")
        signal, reference = row.get("signal_type"), row.get("reference")
        if type(reference) is not str:
            continue
        try:
            timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(UTC)
            parsed_url = urlsplit(reference)
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if (type(event_id) is not int or event_id <= 0 or event_id in event_ids
                or type(user_id) is not int or user_id <= 0 or signal not in _SIGNALS
                or type(row.get("pr_id")) is not int or row["pr_id"] <= 0
                or type(row.get("pr_number")) is not int or row["pr_number"] <= 0
                or parsed_url.scheme != "https" or parsed_url.hostname != "github.com"
                or not parsed_url.path.startswith("/curl/curl/pull/")
                or not start <= timestamp <= end
                or row.get("user_type") == "Bot"
                or (type(row.get("login")) is str and row["login"].lower().endswith("[bot]"))):
            continue
        event_ids.add(event_id)
        records.append({
            "github_user_id": user_id, "signal_type": signal,
            "reference": reference, "event_id": event_id,
            "pr_id": row["pr_id"], "pr_number": row["pr_number"],
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        })
    return sorted(records, key=lambda row: (row["event_id"], row["github_user_id"]))


def load_evidence_snapshot_context() -> dict:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    records = load_recorded_evidence()
    mapping_raw = yaml.safe_load(IDENTITY_MAP_PATH.read_text(encoding="utf-8"))
    load_demo_identity_map()
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return {
        "evidence": records,
        "snapshot_id": hashlib.sha256(canonical(snapshot)).hexdigest(),
        "captured_at": snapshot["captured_at"],
        "identity_mapping_id": hashlib.sha256(canonical(mapping_raw)).hexdigest(),
        "seeded_demo": True,
    }
