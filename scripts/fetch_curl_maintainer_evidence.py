"""Fetch a live curl/curl evidence snapshot, with explicit recorded-snapshot fallback."""

import argparse
import json
import os
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dividend.enrichment.github_client import GitHubClient
from dividend.enrichment.maintainer_evidence import collect_maintainer_evidence

SNAPSHOT = ROOT / "data" / "curl_maintainer_evidence.snapshot.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SNAPSHOT)
    parser.add_argument("--fallback-snapshot", type=Path, default=SNAPSHOT)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing output: {args.output}")
    token = os.environ.get("GITHUB_TOKEN")
    github = GitHubClient(token or "")
    try:
        snapshot = collect_maintainer_evidence(github.client, "https://github.com/curl/curl")
        source = "live GitHub fetch"
    except Exception as exc:
        if not args.fallback_snapshot.is_file():
            status = f" HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
            print(f"GitHub fetch failed ({type(exc).__name__}{status}); no recorded snapshot is available", file=sys.stderr)
            return 1
        try:
            snapshot = json.loads(args.fallback_snapshot.read_text(encoding="utf-8"))
            if (snapshot.get("snapshot_label") != "recorded GitHub maintainer evidence snapshot"
                    or snapshot.get("repository", {}).get("key") != "github.com/curl/curl"
                    or type(snapshot.get("evidence")) is not list):
                raise ValueError("fallback file is not a valid curl recorded snapshot")
        except (OSError, ValueError, TypeError) as fallback_error:
            print(f"GitHub fetch failed ({type(exc).__name__}); fallback failed ({fallback_error})", file=sys.stderr)
            return 1
        source = f"recorded snapshot fallback: {args.fallback_snapshot} (live fetch failed: {type(exc).__name__})"
    finally:
        github.client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Saved {len(snapshot['evidence'])} evidence records from {source} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
