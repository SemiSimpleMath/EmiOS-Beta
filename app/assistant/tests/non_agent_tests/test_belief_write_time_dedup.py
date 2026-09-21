"""The updater preserves records until evidence-aware incremental matching."""
from types import SimpleNamespace
from belief_engine.pipeline.steps.update_beliefs import resolve_domain
from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle, EvidenceItem


def test_existing_belief_keeps_domain():
    assert resolve_domain(agent_domain='food',belief_key='routine.x',existing=SimpleNamespace(domain='health'),
                          valid_domains=['food','health'])=='health'


def test_new_domain_is_validated():
    assert resolve_domain(agent_domain='unknown',belief_key='unknown.x',existing=None,valid_domains=['health']) is None


def test_incoming_source_text_is_complete():
    item=EvidenceItem('daily_insights','2026-01-01','source-id','confirms','summary','x'*5000+' END',3)
    text=EvidenceBundle('all','2026-01-01','2026-01-01',[item]).as_block()
    assert 'x'*5000+' END' in text and 'source-id' in text
