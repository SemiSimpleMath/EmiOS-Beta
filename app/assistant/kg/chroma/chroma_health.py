"""Boot-time chroma corruption self-heal.

Chroma is a DERIVED index (SQLite is truth; embeddings rebuild for free
from the local MiniLM). Its hnsw segments can corrupt — concurrent
writers, an interrupted write, a bad shutdown. A corrupt segment makes
even ``collection.count()`` raise a NATIVE access violation (0xC0000005)
that no try/except can catch and that kills the whole process. Because
``_init_collections`` counts every collection at startup, ONE corrupt
derived index brings the entire server down on boot — a catastrophic
failure mode for an index that is free to rebuild.

This module converts that into self-heal: before the main process opens
chroma, probe each collection's count in an ISOLATED subprocess (a native
crash there exits the child, not us). Any collection whose probe crashes
is dropped; ``_init_collections`` then recreates it empty and the hourly
diff sync / nightly rebuild repopulate it. Healthy boots pay one short
subprocess; corrupt boots pay one extra per corrupt collection.

Three corruptions on 2026-06-12 motivated this; see
scratch/CHROMA_INDEX_FRESHNESS_SPEC.md and the chroma-index-freshness
memory.
"""
from __future__ import annotations

import subprocess
import sys
from typing import List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Every collection the manager creates, in a stable order. The probe walks
# this list; a crash pins the corrupt one as "the first not yet reported OK".
COLLECTION_NAMES = [
    "node_embeddings",
    "edge_embeddings",
    "taxonomy_embeddings",
    "node_context_embeddings",
    "node_identity_embeddings",
    "belief_engine_beliefs",
]

_OK_PREFIX = "CHROMA_OK:"


def _probe_all_main() -> int:
    """Child-process entrypoint: count every existing collection in order,
    printing a flushed marker per success. A corrupt segment crashes the
    process natively before its marker prints, so the parent learns which
    collection died from the last marker seen."""
    import chromadb
    from chromadb.config import Settings

    from app.assistant.utils.path_utils import get_chroma_kg_db_dir

    client = chromadb.PersistentClient(
        path=str(get_chroma_kg_db_dir()),
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )
    existing = {c.name for c in client.list_collections()}
    for name in COLLECTION_NAMES:
        if name not in existing:
            print(f"{_OK_PREFIX}{name}", flush=True)  # absent != corrupt
            continue
        client.get_collection(name).count()  # may access-violate here
        print(f"{_OK_PREFIX}{name}", flush=True)
    return 0


def _drop_collection(name: str) -> None:
    import chromadb
    from chromadb.config import Settings

    from app.assistant.utils.path_utils import get_chroma_kg_db_dir

    client = chromadb.PersistentClient(
        path=str(get_chroma_kg_db_dir()),
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )
    client.delete_collection(name)


def heal_corrupt_collections(*, max_iterations: int = 8) -> List[str]:
    """Probe collections in an isolated subprocess; drop any that crash.

    Returns the list of collection names that were dropped (and will be
    recreated empty by the manager's _init_collections). Idempotent: a
    healthy index drops nothing. MUST run before the main process opens
    chroma, and while no other process touches the index (single writer).
    """
    # Take the cross-process lock before probing/dropping: the heal opens
    # chroma (in probe subprocesses and to drop), so it must be the sole
    # process touching the index. In-server this is the same idempotent
    # lock the manager holds; run standalone while the server is up, this
    # raises ChromaWriterLockError instead of racing it.
    from app.assistant.kg.chroma.chroma_lock import acquire_chroma_writer_lock
    from app.assistant.utils.path_utils import get_chroma_kg_db_dir
    acquire_chroma_writer_lock(get_chroma_kg_db_dir())

    healed: List[str] = []
    for _ in range(max_iterations):
        proc = subprocess.run(
            [sys.executable, "-m", "app.assistant.kg.chroma.chroma_health", "--probe"],
            capture_output=True, text=True,
        )
        reported_ok = [
            line[len(_OK_PREFIX):].strip()
            for line in (proc.stdout or "").splitlines()
            if line.startswith(_OK_PREFIX)
        ]
        if proc.returncode == 0 and len(reported_ok) >= len(COLLECTION_NAMES):
            return healed  # all collections counted cleanly

        # The corrupt collection is the first in canonical order that never
        # reported OK before the child died.
        corrupt = next((n for n in COLLECTION_NAMES if n not in reported_ok), None)
        if corrupt is None:
            logger.error(
                "[chroma_health] probe exited %s but every collection reported OK "
                "(stderr: %s) — not dropping anything", proc.returncode,
                (proc.stderr or "")[-400:],
            )
            return healed
        logger.error(
            "[chroma_health] collection %r is corrupt (probe crashed, exit %s) — "
            "dropping; it will be recreated empty and repopulated by the diff sync.",
            corrupt, proc.returncode,
        )
        try:
            _drop_collection(corrupt)
            healed.append(corrupt)
        except Exception as e:
            logger.error("[chroma_health] drop of %r failed: %s — boot will likely "
                         "crash on init; manual recreate needed", corrupt, e)
            return healed
    logger.error("[chroma_health] still unhealthy after %d heal iterations", max_iterations)
    return healed


if __name__ == "__main__":
    import faulthandler

    faulthandler.enable()
    if "--probe" in sys.argv:
        sys.exit(_probe_all_main())
    # Manual heal: `python -m app.assistant.kg.chroma.chroma_health`
    dropped = heal_corrupt_collections()
    print(f"healed (dropped+to-recreate): {dropped}")
