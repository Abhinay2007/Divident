"""Generate the checked-in synthetic Debian demo fixture set deterministically."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dividend.enrichment.fixtures import FixtureEnricher
from dividend.scan import build_report, parse_rows
from dividend.upstream.mapper import map_source
from dividend.upstream.overrides import load_overrides


PACKAGES = {
    "apt": ("apt", "apt-utils", "libapt-pkg-dev"),
    "bash": ("bash", "bash-builtins"),
    "coreutils": ("coreutils",),
    "dpkg": ("dpkg", "dpkg-dev", "libdpkg-perl"),
    "glibc": ("libc6", "libc-bin", "libc-dev-bin"),
    "systemd": ("systemd", "systemd-sysv", "libsystemd0"),
    "curl": ("curl", "libcurl4", "libcurl4-openssl-dev"),
    "openssl": ("openssl", "libssl3"),
    "zlib": ("zlib1g", "zlib1g-dev"),
    "sqlite": ("sqlite3", "libsqlite3-0", "libsqlite3-dev"),
    "linux": ("linux-image-amd64", "linux-headers-amd64"),
    "libxcb": ("libxcb1", "libxcb1-dev", "libxcb-render0", "libxcb-xfixes0"),
    "xcb-util": ("libxcb-util1",),
    "xcb-util-wm": ("libxcb-icccm4",),
    "xcb-util-image": ("libxcb-image0",),
    "xcb-util-keysyms": ("libxcb-keysyms1",),
    "libxcb-doc": ("libxcb-doc",),
    "linux-signed-amd64": ("linux-signed-amd64",),
}

REPOSITORIES = {
    "apt": "https://salsa.debian.org/apt-team/apt.git",
    "bash": "https://git.savannah.gnu.org/git/bash.git",
    "coreutils": "https://git.savannah.gnu.org/git/coreutils.git",
    "dpkg": "https://git.dpkg.org/cgit/dpkg/dpkg.git",
    "glibc": "https://sourceware.org/git/glibc.git",
    "systemd": "https://github.com/systemd/systemd.git",
    "curl": "https://github.com/curl/curl.git",
    "openssl": "https://github.com/openssl/openssl.git",
    "zlib": "https://github.com/madler/zlib.git",
    "sqlite": "https://github.com/sqlite/sqlite.git",
    "linux": "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
    "libxcb": "https://gitlab.freedesktop.org/xorg/lib/libxcb.git",
    "xcb-util": "https://gitlab.freedesktop.org/xorg/lib/xcb-util.git",
    "xcb-util-wm": "https://gitlab.freedesktop.org/xorg/lib/xcb-util-wm.git",
    "xcb-util-image": "https://gitlab.freedesktop.org/xorg/lib/xcb-util-image.git",
    "xcb-util-keysyms": "https://gitlab.freedesktop.org/xorg/lib/xcb-util-keysyms.git",
}

SIGNALS = {
    "apt": (False, "2026-10-01", 8, True),
    "bash": (False, "2026-09-15", 1, False),
    "coreutils": (False, "2024-01-10", 2, False),
    "dpkg": (False, "2026-08-01", 4, True),
    "glibc": (False, "2025-01-01", 1, False),
    "systemd": (False, "2026-10-20", 20, True),
    "curl": (False, "2026-09-30", 3, True),
    "openssl": (False, "2026-11-01", 8, True),
    "zlib": (True, "2020-01-01", 1, False),
    "sqlite": (False, "2026-07-01", 1, False),
    "linux": (False, "2026-10-15", 50, False),
    "libxcb": (False, "2025-07-04", 2, False),
    "xcb-util": (True, "2024-05-01", 1, False),
    "xcb-util-wm": (False, "2026-08-01", 2, False),
    "xcb-util-image": (False, "2025-01-01", 1, False),
    "xcb-util-keysyms": (False, "2026-09-01", 1, False),
}

# These are synthetic demo payees, not real maintainer identities or accounts.
MAINTAINERS = [
    ("salsa.debian.org/apt-team/apt", True),
    ("git.savannah.gnu.org/git/bash", True),
    ("git.savannah.gnu.org/git/coreutils", False),
    ("git.dpkg.org/cgit/dpkg/dpkg", True),
    ("sourceware.org/git/glibc", True),
    ("github.com/systemd/systemd", True),
    ("github.com/curl/curl", True),
    ("github.com/openssl/openssl", False),
    ("github.com/sqlite/sqlite", True),
    ("gitlab.freedesktop.org/xorg/lib/libxcb", False),
    ("gitlab.freedesktop.org/xorg/lib/xcb-util", True),
    ("gitlab.freedesktop.org/xorg/lib/xcb-util-image", False),
    ("gitlab.freedesktop.org/xorg/lib/xcb-util-keysyms", True),
]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build(output_dir: Path = ROOT / "data") -> None:
    rows = [(binary, source) for source, binaries in PACKAGES.items() for binary in binaries]
    rows.sort(key=lambda row: (row[1], row[0]))
    tsv = "".join(f"ii \t{binary}\t{source}\n" for binary, source in rows)
    (output_dir / "demo_dpkg.tsv").write_text(tsv, encoding="utf-8")
    (ROOT / "dividend" / "data" / "demo_dpkg.tsv").write_text(tsv, encoding="utf-8")

    enriched = {}
    for source, repository in REPOSITORIES.items():
        archived, last_commit, committers, funding_yml = SIGNALS[source]
        enriched[source] = {
            "repository_url": repository,
            "partial": False,
            "github": {
                "full_name": repository.removeprefix("https://").removesuffix(".git"),
                "archived": archived,
                "last_commit": last_commit,
                "committers_12m": committers,
                "funding_yml": funding_yml,
            },
        }
    enriched["libxcb-doc"] = {"partial": True}
    enriched["linux-signed-amd64"] = {"partial": True}
    enrichment = {
        "synthetic_demo_facts": True,
        "sources": enriched,
    }
    _write_json(output_dir / "demo_enrichment.json", enrichment)
    _write_json(ROOT / "dividend" / "data" / "demo_enrichment.json", enrichment)

    report = build_report(rows, "synthetic-demo")
    _write_json(output_dir / "demo_scan.json", report)

    names = sorted(PACKAGES)
    fact_by_name = {fact.source_name: fact for fact in FixtureEnricher(output_dir / "demo_enrichment.json").fetch(names)}
    overrides = load_overrides(ROOT / "data" / "upstream_overrides.yaml")
    mappings = [map_source(name, fact_by_name[name], overrides) for name in names]
    projects: dict[str, list[str]] = {}
    for mapping in mappings:
        if mapping.outcome == "resolved" and mapping.upstream_key is not None:
            projects.setdefault(mapping.upstream_key, []).append(mapping.source_name)
    binaries_by_source = report["sources"]
    _write_json(output_dir / "demo_resolutions.json", {
        "synthetic_demo_facts": True,
        "resolutions": [
            {"source_name": item.source_name, "upstream_key": item.upstream_key,
             "method": item.method, "outcome": item.outcome}
            for item in mappings
        ],
        "projects": [
            {"upstream_key": key, "source_names": sorted(sources),
             "binary_count": sum(len(binaries_by_source[source]) for source in sources)}
            for key, sources in sorted(projects.items())
        ],
    })

    _write_yaml(output_dir / "demo_maintainers.yaml", {
        "schema": 1,
        "identities_are_synthetic": True,
        "maintainers": [
            {"upstream_key": key, "display_name": f"Synthetic demo maintainer ({key.rsplit('/', 1)[-1]})",
             "email": f"demo+{key.rsplit('/', 1)[-1]}@example.invalid", "opted_in": opted_in}
            for key, opted_in in MAINTAINERS
        ],
    })
    _write_yaml(output_dir / "eval_labels.yaml", {
        "schema": 1,
        "synthetic_demo_fixture": True,
        "expected_no_upstream": ["linux-signed-amd64"],
        "expected_unmapped": ["libxcb-doc"],
        "expected_project_count": len(projects),
    })
    sandbox_keys = [key for key, opted_in in MAINTAINERS if opted_in]
    _write_yaml(output_dir / "demo_sandbox_recipients.example.yaml", {
        "schema": 1,
        "recipients": {key: "" for key in sandbox_keys},
    })


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    build()
