"""Complete records and content versions; no semantic decisions or text clipping."""
import hashlib
import json

NON_OBSERVATION_SOURCES = ('canonicalization', 'deprecation', 'weekly_insights', 'decay_review', 'ticket_context')


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def observation_key(ev):
    # Exact source content identity only. Different summaries are never assumed equivalent.
    identity = {k: ev.get(k) for k in (
        "source_type", "source_ref", "source_date", "signal_type", "summary", "raw_text")}
    if not ev.get('source_ref'):
        # Identical wording without source identity can be two independent observations.
        identity['unlinked_evidence_id'] = ev.get('id')
    return digest(identity)


def lineage(conn, belief_id):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pending, seen, records, evidence = [belief_id], set(), [], {}
    while pending:
        bid = pending.pop()
        if bid in seen:
            continue
        seen.add(bid)
        for table in ("user_beliefs", "user_beliefs_archive"):
            if table not in tables:
                continue
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (bid,)).fetchone()
            if row is not None:
                records.append(dict(row))
                break
        for table in ("belief_evidence", "belief_evidence_archive"):
            if table in tables:
                for row in conn.execute(f"SELECT * FROM {table} WHERE belief_id=?", (bid,)):
                    evidence[row['id']] = dict(row)
        if 'belief_merges' in tables:
            pending.extend(r[0] for r in conn.execute(
                "SELECT loser_id FROM belief_merges WHERE survivor_id=?", (bid,)))
    return sorted(records, key=lambda r: r['id']), sorted(evidence.values(), key=lambda r: r['id'])


FIELDS = ('id', 'belief_key', 'statement', 'conditions', 'scope', 'kind', 'domain', 'status', 'locked')


def packet(conn, belief_id):
    row = conn.execute("SELECT * FROM user_beliefs WHERE id=?", (belief_id,)).fetchone()
    if row is None:
        raise ValueError(f"Belief no longer current: {belief_id}")
    records, evidence = lineage(conn, belief_id)
    belief = dict(row)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    evidence_ids = {e['id'] for e in evidence}
    groups = [dict(r) for r in conn.execute('SELECT * FROM belief_observation_equivalence') if r['evidence_id'] in evidence_ids] \
        if 'belief_observation_equivalence' in tables else []
    # Upsert counters, processing timestamps, decay snapshots and repeated identical
    # observations do not represent a new semantic input to matching.
    observations = {observation_key(e): {k: e.get(k) for k in (
        'source_type', 'source_ref', 'source_date', 'signal_type', 'summary', 'raw_text', 'weight', 'valence')}
        for e in evidence if e['source_type'] not in ('canonicalization','deprecation')}
    version = digest({'belief': {k: belief.get(k) for k in FIELDS},
                      'predecessors': [{k: r.get(k) for k in FIELDS} for r in records if r['id'] != belief_id],
                      'observations': observations, 'observation_equivalences': groups})
    return {'belief': belief, 'version': version, 'lineage': records, 'evidence': evidence,
            'observation_equivalences':groups}


def catalog_record(p):
    return {k: p['belief'].get(k) for k in FIELDS}


def observations(conn, belief_id):
    """One contribution per exact or explicitly reviewed source observation."""
    _, evidence = lineage(conn, belief_id)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    aliases = dict(conn.execute('SELECT evidence_id,representative_id FROM belief_observation_equivalence')) \
        if 'belief_observation_equivalence' in tables else {}
    roots = {}
    def root(k):
        while k in roots:
            k = roots[k]
        return k
    exact = {}
    for ev in evidence:
        k = observation_key(ev)
        if k in exact:
            ra, rb = root(ev['id']), root(exact[k])
            if ra != rb:
                roots[ra] = rb
        exact[k] = ev['id']
    for eid, representative in aliases.items():
        ra, rb = root(eid), root(representative)
        if ra != rb:
            roots[ra] = rb
    unique = {}
    for ev in evidence:
        if ev['source_type'] in NON_OBSERVATION_SOURCES:
            continue
        key = root(ev['id'])
        # Preserve uncertainty: duplicate source records never add their weights.
        if key not in unique or float(ev.get('weight') or 0) < float(unique[key].get('weight') or 0):
            unique[key] = ev
    return list(unique.values())


def pages(records, max_chars=48000):
    """Bound batches between whole records. An oversized record is sent whole."""
    batch, size = [], 0
    for record in records:
        length = len(encode(record))
        if batch and size + length > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += length
    if batch:
        yield batch
