---
name: extending-emi-pod-kinds
description: How to add a new pod kind to EmiOS. Pods are URI-addressable memory objects; each kind (image, email, chat_cluster, …) declares whether it auto-mirrors to the KG and how its body is extracted. Use when the task involves adding a new media type, content category, or pod-classified artifact.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
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
`pod_kind_registry` reads the file at startup and the `kg_mirror`
dispatcher checks it on every pod write.

## Pod kind entry

```json
{
  "kinds": {
    "audio_clip": {
      "description": "Short audio recording (≤5 min). Body populated by transcription pass.",
      "auto_mirror_to_kg": true,
      "body_extraction": "transcription",
      "default_for_agents": []
    }
  }
}
```

## Field reference

- **`description`** — human-readable purpose. Shown in admin UI; read
  by no production code path. Worth writing well anyway.
- **`auto_mirror_to_kg`** — bool. When true, every pod of this kind
  gets a Pod node mirrored into the KG at mint time. When false, the
  pod still lives in pod_store and is queryable via `pod_search`; it
  just doesn't get a `kg_node_metadata` row. Fail-closed default:
  unknown kinds do NOT auto-mirror.
- **`body_extraction`** — declarative tag for how `pod.body` gets
  populated. Documentation-only today; future ingest dispatchers can
  branch on this. Known values:
    - `verbatim` — body is set directly at mint time (email, chat_cluster)
    - `vision_pass` — body populated by a vision-extraction pass (image)
    - `transcription` — speech-to-text (audio, video)
    - `ocr` — OCR pass (PDF, scanned document)
    - `none` — pod has no body; metadata-only
- **`default_for_agents`** — list of agent names that should receive
  pods of this kind by default. Mostly empty today; populate when
  it actually matters.

## chat_introduced_source_kinds

A separate top-level list in `pod_kinds.json` declares `source_kind`
values that ALWAYS auto-mirror regardless of pod kind:

```json
"chat_introduced_source_kinds": ["chat_attachment", "manual_mint", "manual_upload"]
```

The principle: when the user introduces a pod into chat (uploads,
pastes, or asks Emi to mint one from disk), the pod is part of the
conversation and must be KG-addressable so extracted relationships
can edge into it — no matter what kind it is. Email-sourced pods
intentionally do NOT trigger this — they go through the normal
allow list and stay out of the KG by default.

If your new kind should also be "always-admit" via specific source
contexts, add the corresponding `source_kind` strings to this list.

## After saving the entry

1. Restart Flask. The registry loads at startup.
2. Verify in logs: `[pod_kind_registry] loaded N kind(s):` includes
   your new kind.
3. If `auto_mirror_to_kg: true`, the next pod of this kind will mint
   a KG Pod node. Otherwise it lives only in pod_store.

## Where new kinds get minted

A pod kind is just a string on `pod.kind`. Code that creates pods
sets the kind directly:

- `image_ingest.py` mints `kind=image`.
- `pod_classifier_service.py` mints `kind=email`, `kind=chat_cluster`,
  `kind=service_loop`.
- A future audio ingest tool would mint `kind=audio_clip` and
  trust the registry to handle KG admission.

The registry doesn't enforce that a kind is registered before it
can be minted (yet) — registration is the agreement about how it
behaves at the KG-mirror boundary.

## Canonical example

`configs/pod_kinds.json` is the live registry. Read the existing
four entries (`image`, `email`, `chat_cluster`, `service_loop`) for
the pattern.

## Notes

- "Email is excluded from auto-mirror" is a deliberate policy choice
  (noisy / sensitive / token-heavy). If the email kind ever flips
  to `auto_mirror_to_kg: true`, also revisit fact-extraction policy
  in `app/assistant/kg_pipeline/`.
- The `body_extraction` field is declarative. There's no central
  dispatcher reading it today — each ingest path knows its own
  extraction strategy. Add the field anyway: it's the documentation
  contract for adding the dispatcher later.
- See also `extending-emi-cameras` for image-pod source registration
  via the camera registry; the camera_dispatcher mints image pods
  through `image_ingest`, which respects this kind registry for KG
  mirror admission.
