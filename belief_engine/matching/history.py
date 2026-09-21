"""Durable discovery and version-bound comparison receipts in the app database."""
from belief_engine.matching.context import NON_OBSERVATION_SOURCES
import sqlite3
import json
from contextlib import contextmanager
from belief_engine.matching.context import encode, digest, packet, observation_key, observations


SCHEMA = '''
CREATE TABLE IF NOT EXISTS belief_match_discovery (
 belief_id TEXT PRIMARY KEY, version TEXT NOT NULL, policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS belief_match_pages (
 receipt_key TEXT PRIMARY KEY, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS belief_match_pairs (
 pair_key TEXT PRIMARY KEY, a_id TEXT NOT NULL, b_id TEXT NOT NULL,
 a_version TEXT NOT NULL, b_version TEXT NOT NULL, policy TEXT NOT NULL,
 decision_json TEXT, context_json TEXT, applied INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS belief_match_merges (
 pair_key TEXT PRIMARY KEY, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 index_synced INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS belief_observation_equivalence (
 evidence_id TEXT PRIMARY KEY, representative_id TEXT NOT NULL, pair_key TEXT NOT NULL);
'''


@contextmanager
def connection(path, initialize=False):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if initialize:
            conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def pair_identity(a, b, policy):
    a, b = sorted((a, b), key=lambda p: p['belief']['id'])
    values = (a['belief']['id'], b['belief']['id'], a['version'], b['version'], policy)
    return digest(values), values


def apply_decision(conn, key, a, b, decision):
    """Commit a reviewed merge plus receipt atomically under a version fence.

    Evidence stays attached to its original belief. The lineage reader exposes it
    through redirects even after archival. A merge creates no observation.
    """
    conn.execute('BEGIN IMMEDIATE')
    for p in (a, b):
        if packet(conn, p['belief']['id'])['version'] != p['version']:
            raise ValueError('Belief or evidence changed during comparison')
    relation = decision['relation']
    if relation in ('same','supersedes','contradicts') and any(p['belief'].get('locked') for p in (a,b)):
        raise ValueError('Cannot change an owner-locked belief')
    if relation == 'same':
        if any(p['belief'].get('locked') for p in (a, b)):
            raise ValueError('Cannot merge an owner-locked belief')
        keep = decision['survivor_id']
        if keep not in (a['belief']['id'], b['belief']['id']):
            raise ValueError('Invalid survivor')
        survivor, loser = (a, b) if keep == a['belief']['id'] else (b, a)
        if not decision.get('canonical_statement', '').strip():
            raise ValueError('Missing canonical statement')
        unique_observations = {observation_key(e): e for p in (a, b) for e in p['evidence']
                        if e['source_type'] not in NON_OBSERVATION_SOURCES}
        dates = [e['source_date'] for e in unique_observations.values()
                 if e.get('source_date') and (e.get('valence') == 'support' if e.get('valence') else e.get('signal_type') == 'confirms')]
        # A legacy record with no dated observations keeps its old date; never use today.
        last = max(dates) if dates else survivor['belief'].get('last_confirmed')
        first_dates = [e['source_date'] for e in unique_observations.values() if e.get('source_date')]
        first = min(first_dates) if first_dates else survivor['belief'].get('first_observed')
        conn.execute('''UPDATE user_beliefs SET statement=?, conditions=?, scope=?, kind=?,
            observation_count=?, last_confirmed=?, first_observed=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id=?''', (decision['canonical_statement'], encode(json.loads(decision['conditions_json']))
                           if decision['conditions_json'] is not None else None,
                           decision['scope'], decision['kind'], len(unique_observations), last, first, keep))
        conn.execute('INSERT OR IGNORE INTO belief_tags(belief_id,tag,assigned_at,method) '
                     'SELECT ?,tag,assigned_at,method FROM belief_tags WHERE belief_id=?',
                     (keep, loser['belief']['id']))
        conn.execute("UPDATE user_beliefs SET status='deprecated' WHERE id=?", (loser['belief']['id'],))
        conn.execute('INSERT OR REPLACE INTO belief_merges(loser_id,survivor_id,merged_at,reason) '
                     "VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
                     (loser['belief']['id'], keep, decision['reason']))
        for group in decision.get('equivalent_observations', []):
            by_id = {e['id']: e for p in (a,b) for e in p['evidence']}
            if len(group) < 2 or not set(group) <= by_id.keys():
                raise ValueError('Invalid observation equivalence group')
            if len({(by_id[e].get('source_date'),by_id[e].get('signal_type')) for e in group}) != 1:
                raise ValueError('Cannot collapse different observation dates or signals')
            representative = min(group)
            for eid in group:
                prior = conn.execute('SELECT representative_id FROM belief_observation_equivalence WHERE evidence_id=?',(eid,)).fetchone()
                if prior and prior[0] != representative:
                    raise ValueError('Conflicting observation equivalence; further review needed')
                conn.execute('INSERT OR IGNORE INTO belief_observation_equivalence VALUES (?,?,?)',
                             (eid, representative, key))
        conn.execute('UPDATE user_beliefs SET observation_count=? WHERE id=?',
                     (len(observations(conn, keep)),keep))
        conn.execute('INSERT INTO belief_match_merges(pair_key,before_json,after_json) VALUES (?,?,?)',
                     (key, encode([a, b]), encode(packet(conn, keep))))
    elif relation in ('supersedes','contradicts'):
        if relation == 'supersedes':
            current = decision['current_id']
            if current not in (a['belief']['id'],b['belief']['id']):
                raise ValueError('Supersession requires a valid current belief')
            outdated = b if current == a['belief']['id'] else a
            conn.execute("UPDATE user_beliefs SET status='deprecated' WHERE id=?", (outdated['belief']['id'],))
        else:
            conn.execute("UPDATE user_beliefs SET status='contested' WHERE id IN (?,?)",(a['belief']['id'],b['belief']['id']))
        conn.execute('INSERT INTO belief_match_merges(pair_key,before_json,after_json) VALUES (?,?,?)',
                     (key,encode([a,b]),encode([packet(conn,a['belief']['id']),packet(conn,b['belief']['id'])])))
    conn.execute('UPDATE belief_match_pairs SET decision_json=?,context_json=?,applied=1 WHERE pair_key=?',
                 (encode(decision), encode([a, b]), key))
