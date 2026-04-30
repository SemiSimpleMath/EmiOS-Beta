# Kg_shared used to re-export a much larger surface; the bulk of those
# exports were node/edge candidate finders + standardized-node creators
# used only by the archived kg_chat_pipeline_parallel chain. Live callers
# in the current codebase need exactly two helpers from merge_utils
# (consumed by kg_maintenance_pipeline/step_execute_findings) and one
# helper from extraction_utils (consumed by the kg_pipeline extractor
# step). Anything else stays package-internal until proven needed.
from .extraction_utils import normalize_extraction_result
from .merge_utils import (
    apply_node_data_merger_result,
    merge_node_fields_into_existing,
)

__all__ = [
    "normalize_extraction_result",
    "apply_node_data_merger_result",
    "merge_node_fields_into_existing",
]

