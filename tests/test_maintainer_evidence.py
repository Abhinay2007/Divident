from datetime import UTC, datetime
import json
from pathlib import Path

import httpx

from dividend.enrichment.maintainer_evidence import collect_maintainer_evidence
from dividend.services.maintainer_evidence import load_demo_identity_map, load_recorded_evidence


def test_collector_paginates_filters_boundaries_bots_missing_ids_and_deduplicates():
    calls = []
    def handler(request):
        calls.append((request.url.path, request.url.params.get("page")))
        path, page = request.url.path, request.url.params.get("page")
        if path == "/repos/curl/curl":
            body = {"full_name": "curl/curl"}
        elif path == "/search/issues" and page == "1":
            body = {"total_count": 101, "items": [
                {"id": 101, "number": 1, "closed_at": "2025-01-01T00:00:00Z", "html_url": "https://github.com/curl/curl/pull/1", "user": {"id": 11, "login": "alice", "type": "User"}},
                {"id": 102, "number": 2, "closed_at": "2024-12-31T23:59:59Z", "html_url": "https://github.com/curl/curl/pull/2", "user": {"id": 12, "login": "old", "type": "User"}},
            ]}
        elif path == "/search/issues" and page == "2":
            body = {"items": [{"id": 103, "number": 3, "closed_at": "2026-01-01T00:00:01Z", "html_url": "https://github.com/curl/curl/pull/3", "user": {"id": 13, "login": "future", "type": "User"}}]}
        elif path == "/repos/curl/curl/pulls/1/reviews" and page == "1":
            body = [
                {"id": 201, "submitted_at": "2025-01-01T00:00:00Z", "html_url": "https://github.com/curl/curl/pull/1#pullrequestreview-201", "user": {"id": 21, "login": "reviewer", "type": "User"}},
                {"id": 202, "submitted_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/curl/curl/pull/1#pullrequestreview-202", "user": {"id": 22, "login": "robot[bot]", "type": "User"}},
                {"id": 203, "submitted_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/curl/curl/pull/1#pullrequestreview-203", "user": {"login": "unknown", "type": "User"}},
                {"id": 101, "submitted_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/curl/curl/pull/1#pullrequestreview-101", "user": {"id": 23, "login": "duplicate", "type": "User"}},
            ]
        else:
            body = []
        return httpx.Response(200, json=body, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = collect_maintainer_evidence(client, "git+https://github.com/curl/curl.git",
                                           datetime(2026, 1, 1, tzinfo=UTC))
    assert [row["event_id"] for row in snapshot["evidence"]] == [101, 201]
    assert [row["github_user_id"] for row in snapshot["evidence"]] == [11, 21]
    assert snapshot["lookback_start"] == "2025-01-01T00:00:00Z"
    assert snapshot["lookback_end"] == "2026-01-01T00:00:00Z"
    assert ("/search/issues", "2") in calls
    assert ("/repos/curl/curl/pulls/1/reviews", "2") in calls
    assert all(row["verified"] is True for row in snapshot["evidence"])


def test_normalization_is_deterministic_and_repository_identity_is_checked():
    def handler(request):
        if request.url.path == "/repos/curl/curl":
            return httpx.Response(200, json={"full_name": "curl/curl"}, request=request)
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "items": []}, request=request)
        return httpx.Response(200, json=[], request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    at = datetime(2026, 1, 1, tzinfo=UTC)
    first = collect_maintainer_evidence(client, "https://github.com/curl/curl", at)
    second = collect_maintainer_evidence(client, "https://github.com/curl/curl", at)
    assert first == second
    assert first["repository"] == {"name": "curl/curl", "url": "https://github.com/curl/curl",
                                   "key": "github.com/curl/curl"}


def test_recorded_snapshot_and_synthetic_identity_map_are_server_loaded(tmp_path):
    mapping = load_demo_identity_map()
    assert mapping[177011] == mapping[1969657] == "github.com/curl/curl"
    snapshot = json.loads(Path("data/curl_maintainer_evidence.snapshot.json").read_text())
    snapshot["evidence"][0]["verified"] = False
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps(snapshot))
    evidence = load_recorded_evidence(path)
    assert evidence
    assert "verified" not in evidence[0]
    snapshot["evidence"].append({**snapshot["evidence"][0], "event_id": 987654, "user_type": "Bot"})
    path.write_text(json.dumps(snapshot))
    assert all(row["event_id"] != 987654 for row in load_recorded_evidence(path))
