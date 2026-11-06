"""Pure allocation domain types and policy configuration."""

from .policy import (
    AllocationPolicy,
    AllocationPolicyError,
    AllocationRules,
    FeePolicy,
    NeedFactorPolicy,
    load_allocation_policy,
)
from .snapshot import build_facts_snapshot, facts_snapshot_from_dict, source_facts_payload
from .engine import (
    AllocationError, AllocationInvariantError, AllocationLine, AllocationResult,
    allocate,
)
from .types import (
    EvidenceItem, FactsSnapshot, GitHubFactsSnapshot, NormalizedFactsSnapshot,
    ProjectFacts, ProjectInput, ResolutionSnapshot, SourceFactsSnapshot,
)

__all__ = [
    "AllocationPolicy",
    "AllocationPolicyError",
    "AllocationRules",
    "FeePolicy",
    "NeedFactorPolicy",
    "ProjectFacts",
    "ProjectInput",
    "EvidenceItem",
    "ResolutionSnapshot",
    "GitHubFactsSnapshot",
    "SourceFactsSnapshot",
    "NormalizedFactsSnapshot",
    "FactsSnapshot",
    "build_facts_snapshot",
    "facts_snapshot_from_dict",
    "source_facts_payload",
    "load_allocation_policy",
    "AllocationError",
    "AllocationInvariantError",
    "AllocationLine",
    "AllocationResult",
    "allocate",
]
