"""Collect public, server-fetched maintainer activity for a GitHub repository."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ..upstream.normalizer import normalize


def collect_maintainer_evidence(client, repository_url: str,
                                captured_at: datetime | None = None) -> dict:
    captured_at = captured_at or datetime.now(UTC)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    captured_at = captured_at.astimezone(UTC)
    repo_key = normalize(repository_url)
    if not repo_key or not repo_key.startswith("github.com/"):
        raise ValueError("repository_url must identify a GitHub repository")
    owner, repo = repo_key.removeprefix("github.com/").split("/")
    base = f"https://api.github.com/repos/{owner}/{repo}"
    metadata_response = client.get(base)
    metadata_response.raise_for_status()
    metadata = metadata_response.json()
    if type(metadata) is not dict or normalize(f"https://github.com/{metadata.get('full_name', '')}") != repo_key:
        raise ValueError("GitHub repository identity did not match the requested repository")

    start = captured_at - timedelta(days=365)
    events = {}

    def pages(url, params=None):
        page = 1
        while True:
            response = client.get(url, params={**(params or {}), "per_page": 100, "page": page})
            response.raise_for_status()
            rows = response.json()
            if type(rows) is not list:
                raise ValueError("GitHub returned an invalid paginated response")
            if not rows:
                break
            yield from rows
            page += 1

    def in_window(value):
        if type(value) is not str:
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
        return timestamp if start <= timestamp <= captured_at else None

    def add(event_id, user, timestamp, signal, reference, pr_number, pr_id):
        if (type(event_id) is not int or type(user) is not dict
                or type(reference) is not str or not reference.startswith("https://github.com/")):
            return
        user_id, login, user_type = user.get("id"), user.get("login"), user.get("type")
        if (type(user_id) is not int or user_id <= 0 or user_type == "Bot"
                or (type(login) is str and login.lower().endswith("[bot]"))):
            return
        events.setdefault(event_id, {
            "maintainer_id": str(user_id), "github_user_id": user_id,
            "signal_type": signal, "reference": reference, "verified": True,
            "event_id": event_id, "pr_number": pr_number, "pr_id": pr_id,
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        })

    query = f"repo:{metadata['full_name']} is:pr is:merged merged:{start.date()}..{captured_at.date()}"
    def search_page(page):
        response = client.get("https://api.github.com/search/issues",
                              params={"q": query, "per_page": 100, "page": page})
        response.raise_for_status()
        return response.json()

    first_page = search_page(1)
    if (type(first_page) is not dict or type(first_page.get("items")) is not list
            or type(first_page.get("total_count")) is not int):
        raise ValueError("GitHub returned an invalid pull request search response")
    if first_page["total_count"] > 1000:
        raise ValueError("GitHub search result limit prevents a complete pull request snapshot")
    pull_requests = list(first_page["items"])
    for page in range(2, (first_page["total_count"] + 99) // 100 + 1):
        payload = search_page(page)
        if type(payload) is not dict or type(payload.get("items")) is not list:
            raise ValueError("GitHub returned an invalid pull request search response")
        pull_requests.extend(payload["items"])

    for pr in pull_requests:
        if type(pr) is not dict or type(pr.get("number")) is not int or type(pr.get("id")) is not int:
            continue
        merged = in_window(pr.get("closed_at"))
        if merged is None:
            continue
        add(pr.get("id"), pr.get("user"), merged, "merged_pull_request",
            pr.get("html_url"), pr["number"], pr["id"])
        for review in pages(f"{base}/pulls/{pr['number']}/reviews"):
            if type(review) is not dict or type(review.get("id")) is not int:
                continue
            submitted = in_window(review.get("submitted_at"))
            if submitted is None:
                continue
            add(review["id"], review.get("user"), submitted, "code_review",
                review.get("html_url"), pr["number"], pr["id"])

    records = sorted(events.values(), key=lambda row: (row["timestamp"], row["event_id"]))
    return {
        "snapshot_label": "recorded GitHub maintainer evidence snapshot",
        "repository": {"name": metadata["full_name"], "url": f"https://github.com/{metadata['full_name']}",
                       "key": repo_key},
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "lookback_start": start.isoformat().replace("+00:00", "Z"),
        "lookback_end": captured_at.isoformat().replace("+00:00", "Z"),
        "evidence": records,
    }
