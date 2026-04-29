# `data/images/` — content-addressed image store

This directory is **managed by Emi**. Don't move or rename files here.

## Layout

```
data/images/<hash[:2]>/<hash>.<ext>
```

Every file is named by the SHA256 of its bytes (first 2 hex chars are
the subdirectory, full hash is the filename stem). Two-char prefix
dirs keep file-system fanout reasonable for ~10⁶ files.

## Why hash names

Content addressing means:

- **Re-ingesting the same image is automatically deduplicated** — same
  bytes → same path.
- **The pod_id for an image pod is a function of the SHA256**, so you
  can recover the pod from the bytes alone.
- **Moves don't need notifications** — there's nothing to notify
  about, the path is derived from the content.

## Don't move files here manually

If you reorganize this directory:

- The pod's `metadata.stored_path` will be stale.
- Reconciler (`app/assistant/pod_store/image_reconcile.py`) can
  recover by re-hashing each file, but that's a CPU-burn fix.

If you need to add an image, use `image_ingest.ingest_image_file()`
which copies into the right hash-named slot. The transport layers
(chat attachment ingest, email attachment ingest, manual upload UI)
all go through that path.

## For curated, name-addressed identity images

Use `resources/identity/` instead. That directory is for entity
profile photos and other named assets where the filename IS semantic.
See `resources/identity/README.md`.

## Sidecars

Each ingested file gets a `<file>.emipod.json` sidecar with its
pod_id and sha256. See `app/assistant/pod_store/file_stamp.py` for
the format. Don't delete sidecars — they let the reconciler identify
files even if hash addressing breaks down (e.g., the user moves a
file out of this directory).
