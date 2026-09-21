"""Incremental LLM discovery and versioned pair review; no weekly all-pairs sweep."""
from belief_engine.db.paths import belief_db_path
from belief_engine.matching.service import run
from app.assistant.ServiceLocator.service_locator import ServiceLocator


class CanonicalizeBeliefSetStep:
    name = 'canonicalize_belief_set'

    def __init__(self, domain=None):
        self.domain = domain

    def inputs(self, ctx):
        return ['db: beliefs, complete evidence, merge lineage, matching receipts']

    def outputs(self, ctx):
        return []

    def run(self, ctx, *, dry_run=False):
        if dry_run:
            raise ValueError('Use an isolated database copy for matching evaluation; no embedding-only merge preview exists')
        from belief_engine.matching.service import sync_indexes
        sync_indexes(belief_db_path())
        result = run(belief_db_path(), ServiceLocator.get('agent_factory'), ctx.scope_context, domain=self.domain)
        sync_indexes(belief_db_path())
        if result['merges']:
            from belief_engine.decay.recompute import recompute_belief_snapshots
            stats = recompute_belief_snapshots(self.domain)
            if stats.errors:
                raise RuntimeError('Post-merge confidence recompute failed')
        ctx.canonicalization_result = result
        return result
