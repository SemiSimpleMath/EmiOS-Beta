"""
Taxonomy data layer (read-only at runtime).

The classification pipeline, the agentic editing surface, and the reviewer
web app were archived on 2026-05-06 to _archived/taxonomy_2026_05_06/.
What remains is the data layer: SQLAlchemy models + read accessors used by
the live `taxonomy_paths` KG-query filter.
"""

from .models import (
    Taxonomy,
    NodeTaxonomyLink,
    TaxonomySuggestion,
    TaxonomySuggestions,
    NodeTaxonomyReviewQueue
)
from .manager import TaxonomyManager

__all__ = [
    'Taxonomy',
    'NodeTaxonomyLink',
    'TaxonomySuggestion',
    'TaxonomySuggestions',
    'NodeTaxonomyReviewQueue',
    'TaxonomyManager',
]

