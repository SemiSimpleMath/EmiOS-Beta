# `resources/identity/` — name-addressed identity images

Curated profile photos and representative images for entities the
user cares about. These are **named by entity**, not by content hash.

## Layout

```
resources/identity/
  jukka.jpg              ← profile photo of Jukka (the user)
  katy.jpg               ← profile photo of Katy
  peter.jpg, annika.jpg  ← children
  bonnie.jpg, waldo.jpg  ← pets
  jukkas_house.jpg       ← representative photo of the house
  ...
```

Filename convention: `<lowercase_entity_label_underscored>.<ext>`.
Common extensions: `.jpg`, `.jpeg`, `.png`, `.heic`, `.webp`.

## How they reach the KG

Each image here gets minted as a pod (`kind=image`) the first time
the reconciler runs over the directory:

```
Jukka --has_profile_image--> datapod:image:<sha256[:24]>
```

The pod's `metadata.stored_path` is `resources/identity/jukka.jpg`
(NOT the content-addressed `data/images/<hash>/...`). For curated
identity images the symbolic name IS the identity — replacing the
photo keeps the same filename and the same edge.

The reconciler also writes a sidecar (`jukka.jpg.emipod.json`) next
to each image with the pod_id, so renames / moves can be detected
and pod metadata updated without re-hashing.

## Why this is separate from `data/images/`

| `resources/identity/` | `data/images/` |
|---|---|
| Named by entity | Hashed by content |
| Stable across photo replacements | Each upload gets a new hash |
| User adds files manually here | Auto-populated by ingest paths |
| ~10-20 files | Potentially thousands |
| `.example` placeholder makes sense | No placeholders |

See `data/images/README.md` for the flow path.

## Don't commit personal photos

The `.gitignore` excludes `resources/identity/*.{jpg,jpeg,png,heic,webp}`.
Only this README is tracked. The `.example` placeholder (if/when
added) ships with the repo so fresh installs see the directory exists.

## Reconciler

```
from app.assistant.pod_store.image_reconcile import reconcile_directory
from app.assistant.utils.path_utils import get_repo_root

reconcile_directory(directory=get_repo_root() / "resources" / "identity",
                    source_kind="curated_identity")
```

Run on demand or via a routine. See
`app/assistant/pod_store/image_reconcile.py` for the full contract.
