"""Read-only provenance recovery proposals. No live writes or confidence updates."""
from pathlib import Path
import json
from belief_engine.matching.context import digest, encode
from belief_engine.matching.service import call_agent


def propose(factory, scope, name, payload, cache_dir):
    if name == 'provenance_review':
        from app.assistant.agents.belief_engine.provenance_review.agent_form import AgentForm
    elif name == 'provenance_discover':
        from app.assistant.agents.belief_engine.provenance_discover.agent_form import AgentForm
    else:
        raise ValueError('Unknown provenance operation')
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    templates = [_jinja_env.loader.get_source(_jinja_env,
        f'belief_engine/{name}/prompts/{part}.j2')[0] for part in ('system','user')]
    identity = digest([name, templates, AgentForm.model_json_schema(), payload])
    path = Path(cache_dir) / (identity + '.json')
    if path.exists():
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt['identity'] != identity or receipt['payload'] != payload:
            raise ValueError('Provenance cache context mismatch')
        result = AgentForm.model_validate(receipt['response']).model_dump()
    else:
        result = call_agent(factory, 'belief_engine::'+name, {'payload':payload}, scope, AgentForm)
    def validate(result):
        if name == 'provenance_review':
            wanted = {s['source_id'] for s in payload['sources']}
            actual = [s['source_id'] for s in result['attributions']]
            if len(actual) != len(set(actual)) or set(actual) != wanted:
                raise ValueError('Attribution must account for every supplied source exactly once: '+encode({
                    'expected':sorted(wanted),'returned':actual}))
            if any(not s['reason'].strip() for s in result['attributions']):
                raise ValueError('Attribution needs a reason')
        else:
            focal = {s['id'] for s in payload['focal']}
            catalog = {s['id'] for s in payload['catalog']}
            def exact_id(value, rows):
                if value in {r['id'] for r in rows}:
                    return value
                matches = [r['id'] for r in rows if r.get('belief_key') == value]
                return matches[0] if len(matches) == 1 else value
            for item in result['candidates']:
                # A unique supplied belief_key is also an exact record identifier.
                # Ambiguous keys and approximate text never resolve here.
                item['belief_id'] = exact_id(item['belief_id'], payload['focal'])
                item['candidate_id'] = exact_id(item['candidate_id'], payload['catalog'])
                if item['belief_id'] not in focal or item['candidate_id'] not in catalog:
                    raise ValueError('Discovery cited an unknown record: '+encode({
                        'returned':item,'allowed_belief_ids':sorted(focal),
                        'allowed_candidate_ids':sorted(catalog)}))
    repair = None
    try:
        validate(result)
    except ValueError as exc:
        if path.exists():
            raise
        repair = {'validation_error': str(exc), 'rejected_response': result}
        result = call_agent(factory, 'belief_engine::'+name,
                            {'payload': {**payload, **repair}}, scope, AgentForm)
        validate(result)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        from uuid import uuid4
        temporary = path.with_suffix('.'+uuid4().hex+'.tmp')
        temporary.write_text(encode({'identity':identity,'payload':payload,'response':result,'repair':repair}),encoding='utf-8')
        temporary.replace(path)
    return result
