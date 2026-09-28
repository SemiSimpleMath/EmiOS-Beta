"""Resumable full-coverage reading for evidence packets too large for one review."""
import json
from app.assistant.utils.logging_config import get_logger
from belief_engine.matching.context import encode, digest, pages
from belief_engine.matching.history import connection

logger = get_logger(__name__)

DIRECT_LIMIT = 160000
FRAGMENT_CHARS = 12000
# The reviewer must return one finding per fragment id on the page, echoed verbatim. That
# burden scales with the NUMBER of fragments, which char bounding does not constrain: pages
# of 30-38 small fragments are where the contract broke (2026-09-25, belief pair
# routine.sleep.recover_tonight_not_day_nap / health.sleep.recent_schedule_improving — 203
# fragments over 6 pages). Lower means more calls but a materially more reliable echo; the
# fragment text dominates cost either way, so only the repeated beliefs block is duplicated.
MAX_FRAGMENTS_PER_PAGE = 12


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


def page_problem(fragments, result):
    """What is wrong with a page review, or None. Coverage first, then empty findings.

    Returns (message, detail) so the caller can both re-ask with the specific ids the model
    got wrong and raise a message that names them. The old check reported only that coverage
    was broken, which told nobody WHICH fragment was missed.
    """
    expected = {f['fragment_id'] for f in fragments}
    findings = result.get('findings') or []
    actual = [f['fragment_id'] for f in findings]
    missing = sorted(expected - set(actual))
    unknown = sorted(set(actual) - expected)
    repeated = sorted({i for i in actual if actual.count(i) > 1})
    if missing or unknown or repeated:
        return ('Evidence-page review omitted or invented a source fragment',
                {'missing_fragment_ids': missing, 'unknown_fragment_ids': unknown,
                 'repeated_fragment_ids': repeated, 'required_fragment_ids': sorted(expected)})
    empty = sorted(f['fragment_id'] for f in findings if not f['findings'].strip())
    if empty:
        return ('Evidence-page review has an empty finding', {'empty_findings_for': empty})
    return None


def _reviewed_page(call_agent, factory, scope, beliefs, fragments, form):
    """One bounded validation repair — the same contract selection.select_records uses.

    A single dropped id used to abort the whole global pass (and, after three nights, disable
    the routine). The model now gets exactly one correction attempt naming the precise ids it
    missed, invented or repeated. No partial acceptance and no fuzzy id matching: full
    coverage is still required, it is just asked for twice before giving up.
    """
    retry = None
    for attempt in range(2):
        result = call_agent(factory, 'belief_engine::evidence_page',
                            {'beliefs': beliefs, 'fragments': fragments, 'page_retry': retry},
                            scope, form)
        problem = page_problem(fragments, result)
        if problem is None:
            return result
        message, detail = problem
        if attempt:
            logger.error('[belief_engine] evidence page still invalid after one correction: %s %s',
                         message, encode(detail))
            raise ValueError(f'{message} (after one correction: {encode(detail)})')
        logger.warning('[belief_engine] evidence page invalid, asking for one correction: %s %s',
                       message, encode(detail))
        retry = {'validation_error': message, **detail}


def review_pair(path, factory, scope, a, b, policy, call_agent, review_form, *, agent_name='belief_engine::match_review', extra=None):
    payload = {'a':a, 'b':b, 'page_reviews':[], **(extra or {})}
    if len(encode(payload)) <= DIRECT_LIMIT:
        return call_agent(factory,agent_name,payload,scope,review_form)
    from app.assistant.agents.belief_engine.evidence_page.agent_form import AgentForm
    beliefs = {'a':a['belief'], 'b':b['belief']}
    reviews = []
    for fragments in pages(fragments_for(a,b), max_chars=36000, max_items=MAX_FRAGMENTS_PER_PAGE):
        key = digest(['evidence-page-v1',policy,beliefs,fragments])
        with connection(path, initialize=True) as conn:
            row = conn.execute('SELECT result_json FROM belief_match_pages WHERE receipt_key=?',(key,)).fetchone()
        if row:
            result = json.loads(row[0])
            cached_problem = page_problem(fragments, result)
            if cached_problem is not None:
                # Receipts are only written after passing, so a stored page that fails now is
                # corrupt rather than a model slip — do not silently re-ask.
                raise ValueError(f'Stored evidence-page receipt is invalid: {cached_problem[0]}')
        else:
            result = _reviewed_page(call_agent, factory, scope, beliefs, fragments, AgentForm)
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
