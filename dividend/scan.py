"""Discover installed Debian packages and group them by source package."""

import subprocess
from collections import defaultdict

QUERY_FORMAT = "${db:Status-Abbrev}\\t${Package}\\t${source:Package}\\n"


def run_dpkg_query():
    """Get package status, binary name, and source package from dpkg."""
    return subprocess.run(
        ["dpkg-query", "-W", "-f", QUERY_FORMAT],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_rows(text):
    """Return (binary, source) pairs for installed packages only."""
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, binary, source = (part.strip() for part in parts)
        if status != "ii" or not binary:
            continue
        rows.append((binary, source or binary))
    return rows


def group_by_source(rows):
    """Group binary packages under their Debian source package."""
    groups = defaultdict(set)
    for binary, source in rows:
        groups[source].add(binary)
    return {source: sorted(binaries) for source, binaries in sorted(groups.items())}


def build_report(rows, origin):
    """Build a deterministic scanner report."""
    sources = group_by_source(rows)
    return {
        "schema": 1,
        "origin": origin,
        "binary_count": sum(len(binaries) for binaries in sources.values()),
        "source_count": len(sources),
        "sources": sources,
    }
