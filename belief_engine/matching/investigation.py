"""Resumable full-coverage reading for evidence packets too large for one review."""
import json
from belief_engine.matching.context import encode, digest, pages
from belief_engine.matching.history import connection

DIRECT_LIMIT = 160000
FRAGMENT_CHARS = 12000


class ReviewBudgetExceeded(ValueError):
    """Complete findings cannot fit; retain the pair without a semantic decision."""
    pass


def fragments_for(a, b):
    fragments = []
    for side, packet in (('a', a), ('b', b)):
        for collection in ('lineage', 'evidence'):
            for position, record in enumerate(packet[collection]):
                serialized = encode(record)
                for offset in range(0, len(serialized), FRAGMENT_CHARS):
                    fragments.append({'fragment_id':f'{side}:{collection}:{position}:{offset}',
                        'side':side, 'collection':collection, 'record_id':record['id'],
                        'offset':offset, 'total_chars':len(serialized),
                        'text':serialized[offset:offset+FRAGMENT_CHARS]})
    return fragments


def review_pair(path, factory, scope, a, b, policy, call_agent, review_form, *, agent_name='belief_engine::match_review', extra=None):
    payload = {'a':a, 'b':b, 'page_reviews':[], **(extra or {})}
    if len(encode(payload)) <= DIRECT_LIMIT:
        return call_agent(factory,agent_name,payload,scope,review_form)
    from app.assistant.agents.belief_engine.evidence_page.agent_form import AgentForm
    beliefs = {'a':a['belief'], 'b':b['belief']}
    reviews = []
    for fragments in pages(fragments_for(a,b), max_chars=36000):
        key = digest(['evidence-page-v1',policy,beliefs,fragments])
        with connection(path, initialize=True) as conn:
            row = conn.execute('SELECT result_json FROM belief_match_pages WHERE receipt_key=?',(key,)).fetchone()
        result = json.loads(row[0]) if row else call_agent(factory,'belief_engine::evidence_page',
            {'beliefs':beliefs,'fragments':fragments},scope,AgentForm)
        expected = {f['fragment_id'] for f in fragments}
        actual = [f['fragment_id'] for f in result['findings']]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError('Evidence-page review omitted or invented a source fragment')
        if any(not f['findings'].strip() for f in result['findings']):
            raise ValueError('Evidence-page review has an empty finding')
        with connection(path) as conn:
            conn.execute('INSERT OR IGNORE INTO belief_match_pages VALUES (?,?)',(key,encode(result)))
        reviews.append({'fragments':[{k:v for k,v in f.items() if k != 'text'} for f in fragments],
                        'findings':result['findings']})
    def described(packet):
        return {'belief':packet['belief'],'version':packet['version'],
                'source_content_location':'Complete source content was read in page_reviews; findings are interpretations.',
                'evidence':[{'id':e['id'],'source_type':e.get('source_type'),'source_date':e.get('source_date'),
                             'source_ref':e.get('source_ref'),'valence':e.get('valence')} for e in packet['evidence']],
                'observation_equivalences':packet.get('observation_equivalences',[])}
    payload = {'a':described(a),'b':described(b),'page_reviews':reviews, **(extra or {})}
    if len(encode(payload)) > DIRECT_LIMIT:
        raise ReviewBudgetExceeded('Complete page findings exceed final review budget; preserved pending without truncation')
    return call_agent(factory,agent_name,payload,scope,review_form)
