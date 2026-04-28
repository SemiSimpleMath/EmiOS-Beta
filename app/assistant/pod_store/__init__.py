"""Pod store — the addressable-content layer for cross-agent evidence.

A pod is a curated semantic unit: URI + one_liner + optional body +
source_refs back to unified_log. Pods are minted by PodClassifier (for
chat/email bursts), by tool runners (for big tool results), or by agents
(for their own synthesis output). Consumers query pod_store by tag to
find relevant context without scanning raw unified_log.

In prose we call these "pods"; in code "datapod" is the full name. The
canonical URI shape is ``datapod:<kind>:<id>`` (e.g.
``datapod:chat_cluster:7467d26404758840b3977ca9``). The ``pod_id``
column on every pod IS that URI — there is no separate URI/id split;
the regex in ``pod_uri.py`` matches the same shape and the result is
fed back to ``PodStore.get(pod_id)`` directly.
"""
from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.pod_store import PodStore

__all__ = ["Pod", "PodSourceRef", "PodStore"]
