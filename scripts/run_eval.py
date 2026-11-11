"""Reproducible offline fixture evaluation for the upstream resolver."""

import argparse
import json
from pathlib import Path

import yaml

from dividend.enrichment.base import FactSet
from dividend.upstream.mapper import map_source
from dividend.upstream.overrides import load_overrides
from dividend.upstream.resolver import cache_key, resolve

ROOT = Path(__file__).resolve().parents[1]


class OfflineMock:
    """Returns deliberately unsupported citations; it is not a model or ground truth."""
    def __init__(self):
        self.calls = 0

    def structured_call(self, prompt, schema, timeout=30):
        rows = json.loads(prompt.split("\n", 1)[1])
        self.calls += 1
        return {"proposals": [{
            "source_name": row["source_name"],
            "upstream_key": "fake.invalid/unverified",
            "display_name": "unverified",
            "evidence": [{"field": "udd.homepage", "value": "https://fake.invalid/unverified"}],
        } for row in rows]}


class CountedProvider:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0
        self.proposals = 0

    def structured_call(self, prompt, schema, timeout=30):
        self.calls += 1
        response = self.provider.structured_call(prompt, schema, timeout)
        self.proposals += len(response.get("proposals", []))
        return response


def _score(predictions, labels, excluded):
    mapped = {name: key for name, key in predictions.items() if key is not None}
    correct = sum(mapped.get(name) == expected for name, expected in labels.items() if expected)
    incorrect = sum(name in mapped and mapped[name] != expected for name, expected in labels.items() if expected)
    unresolved = sum(name not in excluded and predictions.get(name) is None
                     for name in labels)
    eligible = len(labels) - len(excluded)
    return {
        "correct_mappings": correct,
        "incorrect_mappings": incorrect,
        "unresolved_cases": unresolved,
        "intentional_exclusions": len(excluded),
        "coverage": round(correct / eligible, 4) if eligible else 0,
        "precision": round(correct / len(mapped), 4) if mapped else 0,
    }


def evaluate(provider=None):
    fixture = json.loads((ROOT / "data/upstream_eval_cases.json").read_text(encoding="utf-8"))
    labels = {case["source"]: case["expected_key"] for case in fixture["cases"]}
    overrides = load_overrides(ROOT / "data/upstream_overrides.yaml")
    facts = [FactSet(case["source"], case["repository_url"]) for case in fixture["cases"]]
    baseline = {}
    for fact in facts:
        mapping = map_source(fact.source_name, fact, overrides)
        baseline[fact.source_name] = mapping.upstream_key if mapping.outcome == "resolved" else None
    excluded = {case["source"] for case in fixture["cases"] if case.get("outcome") == "no_upstream"}
    unresolved_facts = [fact for fact in facts if fact.source_name not in excluded and baseline[fact.source_name] is None]
    if provider is None:
        provider = OfflineMock()
    counted = CountedProvider(provider)
    stats = {}
    cache = {}
    accepted = resolve(
        unresolved_facts, counted, set(overrides.family_patterns or {}), cache_stats=stats,
        model="offline-mock",
        cache_get=lambda fact, model: cache.get(cache_key(fact, model)),
        cache_set=lambda fact, model, response: cache.__setitem__(cache_key(fact, model), response),
    )
    assisted = dict(baseline)
    assisted.update({proposal.source_name: proposal.upstream_key for proposal in accepted})
    result = {
        "mode": "offline",
        "fixture_purpose": fixture["purpose"],
        "sources": len(labels),
        "methodology": "Expected keys come only from the checked-in fixture. Intentional no_upstream cases are excluded from coverage; unresolved cases remain in its denominator. Precision is correct mappings / all mapped sources. Rejected proposals count each proposal on every retry.",
        "deterministic_only": _score(baseline, labels, excluded),
        "assisted": _score(assisted, labels, excluded),
        "validation_proposals": counted.proposals,
        "validation_rejections": counted.proposals - len(accepted),
        "validation_rejection_rate": round((counted.proposals - len(accepted)) / counted.proposals, 4) if counted.proposals else 0,
        "provider_failures": stats.get("provider_failures", 0),
        "cache_hits": stats.get("cache_hits", 0),
        "cache_lookups": stats.get("cache_hits", 0) + stats.get("cache_misses", 0),
        "cache_hit_rate": round(stats.get("cache_hits", 0) / (stats.get("cache_hits", 0) + stats.get("cache_misses", 0)), 4)
        if stats.get("cache_hits", 0) + stats.get("cache_misses", 0) else 0,
        "llm_calls": getattr(provider, "request_count", counted.calls),
        "representative_failures": [fact.source_name for fact in unresolved_facts],
        "limitations": [
            "The fixture describes resolver contract cases, not a representative Debian sample.",
            "Offline mode uses deliberately invalid mock citations and cannot measure model quality.",
            "No live model run is performed by this evaluation.",
        ],
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(evaluate(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
