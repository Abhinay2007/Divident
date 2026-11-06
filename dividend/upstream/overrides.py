"""Glob-based deterministic package overrides."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class OverrideConfig:
    no_upstream_patterns: tuple[str, ...] = ()
    family_patterns: dict[str, tuple[str, ...]] | None = None


def load_overrides(path: Path | str | None = None) -> OverrideConfig:
    file = Path(path) if path else Path(__file__).resolve().parents[2] / "data" / "upstream_overrides.yaml"
    if not file.exists():
        return OverrideConfig()
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if type(raw) is not dict or set(raw) - {"no_upstream", "families"}:
        raise ValueError("invalid upstream override file")
    excluded, families = raw.get("no_upstream", []), raw.get("families", {})
    if type(excluded) is not list or any(type(x) is not str for x in excluded) or type(families) is not dict:
        raise ValueError("invalid upstream override entries")
    if any(type(k) is not str or type(v) is not list or any(type(x) is not str for x in v) for k, v in families.items()):
        raise ValueError("invalid family override entries")
    return OverrideConfig(tuple(excluded), {k: tuple(v) for k, v in families.items()})


def is_no_upstream(source: str, config: OverrideConfig) -> bool:
    return any(fnmatchcase(source, pattern) for pattern in config.no_upstream_patterns)


def match_family(source: str, config: OverrideConfig) -> str | None:
    return next((key for key, patterns in (config.family_patterns or {}).items()
                 if any(fnmatchcase(source, pattern) for pattern in patterns)), None)
