"""
Taxonomy Classification System

This package contains all taxonomy-related functionality for the Knowledge Graph:
- Database models (Taxonomy, NodeTaxonomyLink, review queues)
- TaxonomyManager for database operations
- TaxonomyOrchestrator for classification logic
- Classification pipeline for batch processing
- Web reviewer for human approval

Usage:
    from app.assistant.kg_core.taxonomy import TaxonomyManager, TaxonomyOrchestrator
    from app.assistant.kg_core.taxonomy.models import Taxonomy, NodeTaxonomyLink
"""

from .models import (
    Taxonomy,
    NodeTaxonomyLink,
    TaxonomySuggestion,
    TaxonomySuggestions,
    NodeTaxonomyReviewQueue
)
from .manager import TaxonomyManager
from .orchestrator import TaxonomyOrchestrator

__all__ = [
    'Taxonomy',
    'NodeTaxonomyLink',
    'TaxonomySuggestion',
    'TaxonomySuggestions',
    'NodeTaxonomyReviewQueue',
    'TaxonomyManager',
    'TaxonomyOrchestrator'
]

