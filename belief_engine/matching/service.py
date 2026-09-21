"""LLM discovery followed by incremental, provenance-aware comparisons."""
from belief_engine.matching.context import NON_OBSERVATION_SOURCES
import json
import logging
from belief_engine.matching.investigation import ReviewBudgetExceeded, DIRECT_LIMIT
from belief_engine.matching.context import packet, catalog_record, pages, digest, encode, FIELDS
from belief_engine.matching.history import connection, pair_identity, apply_decision


def call_agent(factory, name, payload, scope, form):
    from app.assistant.utils.pydantic_classes import Message
    agent = factory.create_agent(name)
    if agent is None:
        raise RuntimeError(f'Missing agent: {name}')
    response = agent.action_handler(Message(agent_input=payload, scope_context=scope))
    return form.model_validate(response.data).model_dump()


def policy_version():
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    from app.assistant.agents.belief_engine.match_discover.agent_form import AgentForm as Discover
    from app.assistant.agents.belief_engine.match_review.agent_form import AgentForm as Review
    sources = [_jinja_env.loader.get_source(_jinja_env, f'belief_engine/{name}/prompts/{part}.j2')[0]
               for name in ('match_discover', 'match_review', 'merge_check', 'evidence_page') for part in ('system', 'user')]
    from app.assistant.agents.belief_engine.merge_check.agent_form import AgentForm as Check
    from app.assistant.agents.belief_engine.evidence_page.agent_form import AgentForm as Page
    return digest(['matching-v3-paged-guard', sources, Discover.model_json_schema(), Review.model_json_schema(), Check.model_json_schema(), Page.model_json_schema()])


def discover(factory, scope, focal, catalog):
    from app.assistant.agents.belief_engine.match_discover.agent_form import AgentForm
    allowed_focal = {r['id'] for r in focal}
    allowed_catalog = {r['id'] for r in catalog}
    def validate(result):
        accepted, seen = [], set()
        for pair in result['pairs']:
            a, b = pair['focal_id'], pair['candidate_id']
            if not {a,b} <= allowed_focal | allowed_catalog:
                raise ValueError('Discovery invented an identity')
            if a == b:
                continue
            if a not in allowed_focal and b in allowed_focal:
                a, b = b, a
            key = tuple(sorted((a,b)))
            if key not in seen:
                accepted.append({**pair,'focal_id':a,'candidate_id':b})
                seen.add(key)
        return {**result,'pairs':accepted}
    result = call_agent(factory, 'belief_engine::match_discover',
                        {'focal': focal, 'catalog': catalog}, scope, AgentForm)
    try:
        return validate(result)
    except ValueError:
        # One bounded retry with compact identity labels avoids UUID transcription
        # errors. Labels are local to this exact page and carry no semantic meaning.
        ids = list(dict.fromkeys(r['id'] for r in [*focal,*catalog]))
        labels = {bid:f'B{i}' for i,bid in enumerate(ids)}
        reverse = {label:bid for bid,label in labels.items()}
        result = call_agent(factory, 'belief_engine::match_discover',
            {'focal':[{**r,'id':labels[r['id']]} for r in focal],
             'catalog':[{**r,'id':labels[r['id']]} for r in catalog]},scope,AgentForm)
        restored = []
        for pair in result['pairs']:
            if pair['focal_id'] not in reverse or pair['candidate_id'] not in reverse:
                raise ValueError('Discovery invented an identity on retry')
            restored.append({**pair,'focal_id':reverse[pair['focal_id']],
                             'candidate_id':reverse[pair['candidate_id']]})
        return validate({**result,'pairs':restored})


def select_for_evidence(store, factory, scope, evidence):
    """Expose relevant existing beliefs to the updater without a top-k embedding gate."""
    from dataclasses import asdict
    from app.assistant.agents.belief_engine.evidence_match.agent_form import AgentForm
    from belief_engine.db.paths import belief_db_path
    records = {b.belief_key: asdict(b) for b in store.list_all(status='active')}
    catalog = [{k: record.get(k) for k in FIELDS} for record in records.values()]
    selected = set()
    for evidence_page in pages(evidence):
        for catalog_page in pages(catalog):
            from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
            prompt = [_jinja_env.loader.get_source(_jinja_env, f'belief_engine/evidence_match/prompts/{part}.j2')[0]
                      for part in ('system','user')]
            key = digest(['evidence-selection-v2-local-ids', prompt, AgentForm.model_json_schema(), evidence_page, catalog_page])
            with connection(belief_db_path(), initialize=True) as conn:
                cached = conn.execute('SELECT result_json FROM belief_match_pages WHERE receipt_key=?', (key,)).fetchone()
            if cached:
                out = json.loads(cached[0])
            else:
                from belief_engine.matching.selection import select_records
                from app.assistant.utils.pydantic_classes import Message
                def invoke(labeled, retry):
                    agent = factory.create_agent('belief_engine::evidence_match')
                    if agent is None:
                        raise RuntimeError('Missing agent: belief_engine::evidence_match')
                    response = agent.action_handler(Message(scope_context=scope, agent_input={
                        'evidence': evidence_page, 'catalog': labeled, 'selection_retry': retry}))
                    return getattr(response, 'data', None)
                chosen, reasoning = select_records(catalog_page, invoke, AgentForm)
                out = {'belief_keys': [r['belief_key'] for r in chosen], 'reasoning': reasoning}
            allowed = {r['belief_key'] for r in catalog_page}
            if not set(out['belief_keys']) <= allowed:
                raise ValueError('Evidence matching returned an unknown belief key')
            selected.update(out['belief_keys'])
            with connection(belief_db_path()) as conn:
                conn.execute('INSERT OR IGNORE INTO belief_match_pages VALUES (?,?)', (key, encode(out)))
    # Current complete claims/conditions are the update surface. Historical source
    # packets belong to focused reconciliation, never a union of every selected trail.
    return [records[k] for k in sorted(selected)]


def validate_review(a, b, decision):
    refs = {e['id'] for p in (a,b) for e in p['evidence']}
    if not set(decision['evidence_ids']) <= refs:
        raise ValueError('Reviewer invented evidence')
    if decision['relation'] in ('same','supersedes','contradicts') and not decision['evidence_ids']:
        raise ValueError('Belief mutation requires cited evidence')
    if decision['relation'] == 'same':
        for p in (a,b):
            source_ids = {e['id'] for e in p['evidence'] if e['source_type'] not in NON_OBSERVATION_SOURCES}
            if not source_ids.intersection(decision['evidence_ids']):
                raise ValueError('Merge requires original evidence from both sides')
    evidence = {e['id']:e for p in (a,b) for e in p['evidence']}
    for group in decision.get('equivalent_observations', []):
        if decision['relation'] != 'same' or len(set(group)) < 2 or not set(group) <= evidence.keys():
            raise ValueError('Invalid observation equivalence group')
        if len({(evidence[e].get('source_date'),evidence[e].get('signal_type')) for e in group}) != 1:
            raise ValueError('Observation equivalence crosses source dates or signals')
    return decision


def review(factory, scope, a, b, *, path=None, policy=None):
    """Validate references; retry one invalid response with exact local source labels."""
    from copy import deepcopy
    from app.assistant.agents.belief_engine.match_review.agent_form import AgentForm
    def invoke(left, right):
        if path is None:
            return call_agent(factory,'belief_engine::match_review',{'a':left,'b':right},scope,AgentForm)
        from belief_engine.matching.investigation import review_pair
        return review_pair(path,factory,scope,left,right,policy,call_agent,AgentForm)
    decision = invoke(a,b)
    try:
        return validate_review(a,b,decision)
    except ValueError:
        ids = list(dict.fromkeys(e['id'] for p in (a,b) for e in p['evidence']))
        labels = {eid:f'E{i}' for i,eid in enumerate(ids)}
        reverse = {label:eid for eid,label in labels.items()}
        packets = deepcopy([a,b])
        for p in packets:
            for e in p['evidence']:
                e['id'] = labels[e['id']]
            for group in p.get('observation_equivalences',[]):
                for field in ('evidence_id','representative_id'):
                    if group.get(field) in labels:
                        group[field] = labels[group[field]]
        decision = invoke(packets[0],packets[1])
        decision['evidence_ids'] = [reverse.get(e,e) for e in decision['evidence_ids']]
        decision['equivalent_observations'] = [[reverse.get(e,e) for e in group]
                                               for group in decision.get('equivalent_observations',[])]
        return validate_review(a,b,decision)


def check_merge(factory, scope, a, b, proposal, *, path=None, policy=None):
    if proposal['relation'] not in ('same','supersedes'):
        return proposal
    from app.assistant.agents.belief_engine.merge_check.agent_form import AgentForm
    if path is None:
        check = call_agent(factory,'belief_engine::merge_check',{'a':a,'b':b,'proposal':proposal},scope,AgentForm)
    else:
        from belief_engine.matching.investigation import review_pair
        check = review_pair(path,factory,scope,a,b,policy,call_agent,AgentForm,
                            agent_name='belief_engine::merge_check',extra={'proposal':proposal})
    originals = {e['id'] for p in (a,b) for e in p['evidence'] if e['source_type'] not in NON_OBSERVATION_SOURCES}
    errors = []
    if not set(check['evidence_ids']) <= originals:
        errors.append('Merge check cited unknown or bookkeeping evidence')
    if check['verdict'] == 'approve':
        for p in (a,b):
            if not {e['id'] for e in p['evidence'] if e['source_type'] not in NON_OBSERVATION_SOURCES}.intersection(check['evidence_ids']):
                errors.append('Merge check lacks original evidence from both sides')
    if check['verdict'] == 'approve' and not check['changed_meanings'] and not errors:
        return {**proposal,'merge_check':check}
    # No valid positive approval: retain both claims, including the complete rejected
    # proposal and guard diagnostics. This is not evidence that either claim is false.
    return {**proposal,'relation':'unresolved','reason':check['reason'],
            'equivalent_observations':[],'merge_check':check,'merge_check_errors':errors,
            'proposed_merge':proposal}


def run(path, factory, scope, *, domain=None, discovery_limit=20, comparison_limit=100):
    """Bound work, not text. Durable queues resume on later pipeline runs."""
    policy = policy_version()
    with connection(path, initialize=True) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM user_beliefs WHERE status='active' ORDER BY id")]
        packets = {bid: packet(conn, bid) for bid in ids}
        receipts = {r['belief_id']: (r['version'], r['policy'])
                    for r in conn.execute('SELECT * FROM belief_match_discovery')}
    dirty = [p for bid, p in packets.items() if receipts.get(bid) != (p['version'], policy)
             and (domain is None or p['belief']['domain'] == domain)]
    dirty.sort(key=lambda p: (p["belief"]["id"] in receipts, p["belief"]["id"]))
    focal = dirty[:discovery_limit]
    stats = {'discovered': 0, 'compared': 0, 'merges': 0, 'pending_discovery': len(dirty)-len(focal), 'blocked_review_budget': 0}
    catalog = [catalog_record(p) for p in packets.values()]
    # Receipt per complete page: an interruption never makes us pay again for
    # successfully persisted discovery pages with identical inputs.
    # Discovery uses complete belief records; full evidence is read at pair review.
    # Repeating archived histories against every catalog page multiplies cost without
    # authorizing any additional decision at this candidate-only stage.
    for focal_page in pages([p['belief'] for p in focal], max_chars=48000):
        focal_ids = [r['id'] for r in focal_page]
        for catalog_page in pages(catalog):
            page_key = digest([policy, [(bid, packets[bid]['version']) for bid in focal_ids], catalog_page])
            with connection(path) as conn:
                cached = conn.execute('SELECT result_json FROM belief_match_pages WHERE receipt_key=?', (page_key,)).fetchone()
            result = json.loads(cached[0]) if cached else discover(factory, scope, focal_page, catalog_page)
            with connection(path) as conn:
                for pair in result['pairs']:
                    a, b = packets[pair['focal_id']], packets[pair['candidate_id']]
                    key, values = pair_identity(a, b, policy)
                    conn.execute('INSERT OR IGNORE INTO belief_match_pairs '
                                 '(pair_key,a_id,b_id,a_version,b_version,policy) VALUES (?,?,?,?,?,?)', (key, *values))
                conn.execute('INSERT OR IGNORE INTO belief_match_pages VALUES (?,?)', (page_key, encode(result)))
        with connection(path) as conn:
            for bid in focal_ids:
                if packet(conn, bid)['version'] != packets[bid]['version']:
                    raise ValueError('Belief changed during discovery')
                conn.execute('INSERT OR REPLACE INTO belief_match_discovery VALUES (?,?,?)',
                             (bid, packets[bid]['version'], policy))
                stats['discovered'] += 1
    with connection(path) as conn:
        queue = [dict(r) for r in conn.execute('SELECT * FROM belief_match_pairs WHERE applied=0 ORDER BY rowid')]
    attempted = 0
    for item in queue:
        if attempted >= comparison_limit:
            break
        with connection(path) as conn:
            current = [conn.execute("SELECT status FROM user_beliefs WHERE id=?", (item[k],)).fetchone()
                       for k in ('a_id','b_id')]
            if any(r is None or r[0] != 'active' for r in current):
                conn.execute('UPDATE belief_match_pairs SET applied=1, decision_json=? WHERE pair_key=?',
                             (encode({'relation':'stale', 'reason':'Belief no longer active'}),item['pair_key']))
                continue
            a, b = packet(conn, item['a_id']), packet(conn, item['b_id'])
            if (a['version'], b['version'], policy) != (item['a_version'],item['b_version'],item['policy']):
                conn.execute('UPDATE belief_match_pairs SET applied=1, decision_json=? WHERE pair_key=?',
                             (encode({'relation':'stale','reason':'Inputs changed; rediscovery required'}),item['pair_key']))
                continue
        # A budget receipt is not a semantic decision. Retry when inputs, policy,
        # or the review budget change; unchanged oversized pairs make no model calls.
        receipt = json.loads(item['decision_json']) if item['decision_json'] else {}
        if receipt.get('relation') == 'pending_review_budget' and receipt.get('budget') == DIRECT_LIMIT:
            stats['blocked_review_budget'] += 1
            continue
        attempted += 1
        try:
            decision = review(factory, scope, a, b, path=path, policy=policy)
            decision = check_merge(factory, scope, a, b, decision, path=path, policy=policy)
        except ReviewBudgetExceeded as exc:
            with connection(path) as conn:
                conn.execute('UPDATE belief_match_pairs SET decision_json=? WHERE pair_key=?',
                    (encode({'relation':'pending_review_budget', 'budget':DIRECT_LIMIT, 'reason':str(exc)}), item['pair_key']))
            stats['blocked_review_budget'] += 1
            logging.getLogger(__name__).warning('Belief pair %s remains pending: %s', item['pair_key'], exc)
            continue
        with connection(path) as conn:
            apply_decision(conn, item['pair_key'], a, b, decision)
        stats['compared'] += 1
        stats['merges'] += decision['relation'] == 'same'
    with connection(path) as conn:
        stats['pending_comparisons'] = conn.execute('SELECT COUNT(*) FROM belief_match_pairs WHERE applied=0').fetchone()[0]
        receipts = {r['belief_id']:(r['version'],r['policy']) for r in conn.execute('SELECT * FROM belief_match_discovery')}
        stats['pending_discovery'] = sum(receipts.get(r['id']) != (packet(conn,r['id'])['version'],policy)
            for r in conn.execute("SELECT id,domain FROM user_beliefs WHERE status='active'").fetchall()
            if domain is None or r['domain'] == domain)
    stats['status'] = 'pending' if stats['pending_discovery'] or stats['pending_comparisons'] else 'ok'
    return stats


def sync_indexes(path):
    """Retry derived-index writes after committed merges; SQL remains authoritative."""
    with connection(path, initialize=True) as conn:
        pending = [dict(r) for r in conn.execute('SELECT * FROM belief_match_merges WHERE index_synced=0')]
    if not pending:
        return
    from belief_engine.chroma.belief_chroma import get_belief_chroma
    index = get_belief_chroma()
    for row in pending:
        original = json.loads(row['before_json'])
        with connection(path) as conn:
            for p in original:
                current = conn.execute('SELECT * FROM user_beliefs WHERE id=?', (p['belief']['id'],)).fetchone()
                if current is not None and current['status'] == 'active':
                    index.upsert(belief_id=current['id'], statement=current['statement'], domain=current['domain'])
                else:
                    index.delete_many([p['belief']['id']])
            conn.execute('UPDATE belief_match_merges SET index_synced=1 WHERE pair_key=?', (row['pair_key'],))
