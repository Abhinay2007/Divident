"""Structured metadata enrichment for upstream resolution."""

from .base import Enricher, FactSet, GitHubFacts
from .live import LiveEnricher

__all__ = ["Enricher", "FactSet", "GitHubFacts", "LiveEnricher"]
