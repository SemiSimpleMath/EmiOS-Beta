---
name: extending-emi-pod-kinds
description: How to add a new pod kind to EmiOS. Pods are URI-addressable memory objects; each kind (image, email, chat_cluster, intention.meal, …) is registered in configs/pod_kinds.json, which gates minting and KG admissibility. Use when the task involves adding a new media type, content category, or pod-classified artifact.
license: Apache-2.0
metadata:
  author: emi-team
  version: "2.0"
  auto_inject_when:
    task_keywords:
      - "new pod kind"
      - "add pod kind"
      - "pod type"
      - "extend emi pods"
      - "audio pod"
      - "video pod"
      - "document pod"
---

# Adding a new pod kind

Pod kinds are declarative entries in `configs/pod_kinds.json`. The
`pod_kind_registry` reads the file at startup, `PodStore.put` refuses
new pods whose kind is unregistered (register FIRST, mint second), and
the KG promoter consults `kg_admissible` before writing any edge with a
`datapod:*` endpoint.

## Kind name + pod id format (the canonical grammar)

A kind name is snake_case segments joined by dots — the dot is the
namespace separator: `chat_cluster`, `intention.meal`, `auth.bearer`.

Every pod id is `datapod:<kind>:<id>` where `<kind>` is exactly the
pod's kind field and `<id>` is a lowercase `[a-z0-9_]` token, 6+ chars.
Build ids with `pod_utils.canonical_pod_id(kind, *parts)` — it hashes
the parts to a deterministic token, so re-minting the same logical unit
upserts ONE pod, and the id is recognized everywhere a reference can
appear (the PodInjector, the chat linkifier, `/pod expand`).
`PodStore.put` enforces this grammar on every new pod, so the format
holds no matter who minted. Filenames, titles, and other free text
belong in `metadata`, never in the id.

## Pod kind entry

```json
{
  "kinds": {
    "audio_clip": {
      "description": "Short audio recording (≤5 min). Body populated by transcription pass.",
      "kg_admissible": true,
      "body_extraction": "transcription",
      "default_for_agents": [],
      "retention": {"mode": "keep_days", "days": 180}
    }
  }
}
```

## Field reference

- **`description`** — human-readable purpose. Shown in admin UI; read
  by no production code path. Worth writing well anyway.
- **`kg_admissible`** — bool. When true, KG edges may reference pods of
  this kind by URI (a person `depicted_in datapod:image:…`). pod_store
  stays the sole source of truth for content — nothing is mirrored into
  the KG. Fail-closed default: unknown kinds are NOT admissible.
- **`body_extraction`** — declarative tag for how `pod.body` gets
  populated. Documentation-only today; future ingest dispatchers can
  branch on this. Known values:
    - `verbatim` — body is set directly at mint time (email, chat_cluster)
    - `vision_pass` — body populated by a vision-extraction pass (image)
    - `transcription` — speech-to-text (audio, video)
    - `ocr` — OCR pass (PDF, scanned document)
    - `none` — pod has no body; metadata-only
- **`default_for_agents`** — list of agent names that should receive
  pods of this kind by default. Informational today.
- **`retention`** — how long pods of this kind stay before the nightly
  retention sweep removes them. `{"mode": "keep_forever"}` (the default
  when omitted), or `{"mode": "keep_days", "days": N}` optionally with
  `"keep_latest": M` (the newest M survive regardless of age). Pods
  with projection rows (secrets) are never swept.

## chat_introduced_source_kinds

A separate top-level list in `pod_kinds.json` declares `source_kind`
values whose pods are ALWAYS KG-admissible regardless of pod kind:

```json
"chat_introduced_source_kinds": ["chat_attachment", "manual_mint", "manual_upload"]
```

The principle: when the user introduces a pod into chat (uploads,
pastes, or asks the assistant to mint one from disk), the pod is part
of the conversation and must be KG-addressable so extracted
relationships can edge into it — no matter what kind it is.
Email-sourced pods intentionally do NOT trigger this — they go through
the normal `kg_admissible` flag and stay out of the KG by default.

## After saving the entry

1. Restart Flask. The registry loads at startup.
2. Verify in logs: `[pod_kind_registry] loaded N kind(s):` includes
   your new kind.
3. Mint through your ingest path with `canonical_pod_id`. An
   unregistered kind fails loudly at `PodStore.put` — that error means
   the registry entry is missing, and the fix is step 1.

## Where new kinds get minted

A pod kind is a registered string on `pod.kind`. Code that creates
pods sets the kind directly:

- `image_ingest.py` mints `kind=image`.
- `pod_classifier_service.py` mints `kind=email`, `kind=chat_cluster`.
- The subconscious lanes mint the `intention.*` / `plan.*` fleet.
- Secret pods (`identity.*`, `auth.*`) come from deterministic
  materializers via `put_secret_pod` — never from an LLM tool.
- A future audio ingest tool would mint `kind=audio_clip` and trust
  the registry for KG admission + retention.

## Canonical example

`configs/pod_kinds.json` is the live registry. Read the existing
entries for the pattern.

## Notes

- "Email is excluded from the KG" is a deliberate policy choice
  (noisy / sensitive / token-heavy). If the email kind ever flips to
  `kg_admissible: true`, also revisit fact-extraction policy in
  `app/assistant/kg_pipeline/`.
- The `body_extraction` field is declarative. There's no central
  dispatcher reading it today — each ingest path knows its own
  extraction strategy. Add the field anyway: it's the documentation
  contract for adding the dispatcher later.
- See also `extending-emi-cameras` for image-pod source registration
  via the camera registry; the camera_dispatcher mints image pods
  through `image_ingest`, which respects this kind registry for KG
  admission.
