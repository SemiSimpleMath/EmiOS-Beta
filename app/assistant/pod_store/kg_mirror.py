"""Mirror pods into kg_node_metadata so KG edges can target them.

Every pod gets a thin ``kg_node_metadata`` row written alongside it:
  - ``id``: the pod's URI (datapod:<kind>:<id>)
  - ``node_type``: "Pod"
  - ``label``: pod.one_liner (truncated)
  - ``category``: pod.kind (chat_cluster, email, image, video, ...)
  - ``original_sentence``: first ~200 chars of pod.body
  - ``source``: "pod_mirror"
  - ``created_at`` / ``updated_at``: managed by SQLAlchemy defaults

The mirror is a denormalized projection — pod_store is still the
source of truth for body, tags, source_refs, and metadata. The
kg_node row exists so:

1. **Edges can target pods.** ``kg_edge_metadata.target_id`` accepts
   any value present in ``kg_node_metadata.id``. Mint a thin Pod node
   and edges from Entity / Event / State nodes can attach: e.g.
   ``Katy --depicted_in--> datapod:image:abc...``,
   ``Peter's Birth --has_video--> datapod:video:def...``.

2. **Existing tools work unchanged.** Graph viz, kg_query, wiki
   neighborhood walks, kg_investigator, all already speak
   "kg_node_metadata id". Pods slot in without any of those caring.

3. **Multi-anchor falls out for free.** A single video pod can be
   edged by Peter, the Birth event, the date, the location — five
   queries finding the same pod via different walks.

Edge-direction convention: KG-node → Pod. The KG node owns / contains
/ documents-via the pod. Reverse direction is fine when semantically
appropriate, but most cases read more naturally as
``Katy --depicted_in--> Pod`` than ``Pod --depicts--> Katy``.

Idempotency: ``ensure_pod_node()`` is safe to call repeatedly. Re-runs
on an existing pod refresh label / original_sentence / category if
the pod's content changed; nothing else.
"""
from __future__ import annotations

from typing import Optional

from app.assistant.kg.db.knowledge_graph_db import Node, get_session
from app.assistant.pod_store.contracts import Pod
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


_LABEL_MAX = 200          # kg_node_metadata.label is TEXT but keep readable
_ORIGINAL_SENTENCE_MAX = 400


def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def ensure_pod_node(pod: Pod) -> str:
    """Insert or refresh the kg_node_metadata mirror row for a pod.

    Returns the node id (== pod.pod_id). Caller doesn't need this — it's
    handy for tests and follow-on edge writes.

    Refresh policy: if the row already exists, update label / original_sentence /
    category to match the pod's current state. ``id`` and ``node_type`` are
    immutable. Other kg_node fields (importance, dates, attributes) are
    untouched — they're either unset (typical for Pod nodes) or have been
    set by an upstream agent and we shouldn't clobber.
    """
    if not pod.pod_id:
        raise ValueError("ensure_pod_node: pod.pod_id is required")

    label = _truncate(pod.one_liner or pod.pod_id, _LABEL_MAX) or pod.pod_id
    original_sentence = _truncate(pod.body, _ORIGINAL_SENTENCE_MAX) or None
    category = (pod.kind or "").strip() or None

    session = get_session()
    try:
        node = session.query(Node).filter_by(id=pod.pod_id).one_or_none()
        if node is None:
            node = Node(
                id=pod.pod_id,
                label=label,
                node_type="Pod",
                category=category,
                original_sentence=original_sentence,
                source="pod_mirror",
                attributes={},
            )
            session.add(node)
            session.commit()
            logger.debug("[kg_mirror] minted Pod node %s (%s)", pod.pod_id[:30], category or "?")
            return pod.pod_id

        # Refresh in-place
        changed = False
        if node.label != label:
            node.label = label
            changed = True
        if node.original_sentence != original_sentence:
            node.original_sentence = original_sentence
            changed = True
        if node.category != category:
            node.category = category
            changed = True
        if changed:
            session.commit()
            logger.debug("[kg_mirror] refreshed Pod node %s (%s)", pod.pod_id[:30], category or "?")
        return pod.pod_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def backfill_existing_pods(*, pod_store=None) -> dict:
    """One-shot: walk pod_store, ensure a Pod node exists for each.

    Used at adoption time (right after this module ships) so existing
    chat_cluster / email pods become KG citizens without re-minting.
    Returns ``{"scanned": int, "minted": int, "refreshed": int}``.
    """
    from app.assistant.pod_store.pod_store import PodStore

    store = pod_store or PodStore()
    # PodStore doesn't expose "iter all pods" today; query with no filters
    # is the closest. limit=10000 is generous — current install has dozens.
    all_pods = store.query(limit=10000)

    minted = 0
    refreshed = 0
    for pod in all_pods:
        existed_session = get_session()
        try:
            existed = existed_session.query(Node).filter_by(id=pod.pod_id).one_or_none() is not None
        finally:
            existed_session.close()
        ensure_pod_node(pod)
        if existed:
            refreshed += 1
        else:
            minted += 1
    return {"scanned": len(all_pods), "minted": minted, "refreshed": refreshed}
