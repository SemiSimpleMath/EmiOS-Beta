# Pods: media lifecycle

End-to-end trace of what happens when the user pastes an image (or
attaches an audio / video file) in chat, through to the moment they
say "find that picture and email it to Katy" and Gmail sends the
right file. This page is the concrete walkthrough; see
[14_PODS.md](14_PODS.md) for the underlying primitives (gut,
classifier, store, search/fetch).

## Two phases

The lifecycle has two distinct halves with different agents in each:

1. **Ingest** — image arrives in chat, becomes a pod, lands in the KG
   with edges from the right entities.
2. **Retrieval + action** — user asks a follow-up referring to the
   image, an agent finds the right pod, attaches the file to an email.

Phase 1 happens on every upload, asynchronously through the kg_pipeline.
Phase 2 happens on demand, synchronously inside whichever manager handles
the user's chat turn.

## Phase 1: image → pod → KG edges

```
Browser:
  user pastes image, types "Hey Emi here is a picture of me"
  POST /process_request with multipart (text + file)
        │
        ▼
app/routes/process_request.py
  • validates mime/size, secure_filename
  • prepare_chat_image() resizes to chat dims, drops EXIF
  • ingest_image_file(processed_path, source_kind="chat_attachment")
        │
        ▼
app/assistant/pod_store/image_ingest.py
  • SHA256 the bytes
  • copy into data/images/<hash[:2]>/<hash>.<ext>          [content-addressed]
  • write_stamp(): sidecar JSON pod_id + sha256
  • PodStore.put(Pod(kind="image", body="", one_liner="[image: jpeg, 320kb, 1920x1080]",
                     metadata={sha256, stored_path, width, height, source_kind, ...}))
        │
        ▼
app/assistant/pod_store/pod_store.py::PodStore.put
  • upsert pod_store row (pod_id deterministic from sha256)
  • triggers kg_mirror.ensure_pod_node()
        │
        ▼
app/assistant/pod_store/kg_mirror.py
  • upsert kg_node_metadata row:
        id           = pod_id (e.g. datapod:image:abc12345...)
        node_type    = "Pod"
        category     = "image"
        label        = pod.one_liner
        original_sentence = pod.body[:400]   (empty until vision extraction runs)
        source       = "pod_mirror"
  • the Pod is now a first-class KG citizen — edges can target it
        │
        ▼
process_request.py::handle_normal_processing
  • metadata["attachments"][0]["pod_id"] = pod.pod_id
  • _run_room_inbound() → DI.room_session_manager.handle_ui_inbound(metadata=...)
        │
        ▼
app/assistant/room_session_manager/services/surfaces/ui_inbound_service.py
  • reads metadata["attachments"]
  • appends the naked pod URI to the chat body:
        "Here is a picture of me datapod:image:abc12345..."
    (falls back to "[emi_image: <path>]" only if ingest_image_file failed)
  • builds InboundEnvelope with the assembled body, persists into unified_log_2026
        │
        ▼
chat agent loop (master_room) sees the message; the planner may reply
inline. Independent of the chat reply, the kg_pipeline picks up the
new unified_log row in its next tick.
        │
        ▼
app/assistant/pipelines/kg_pipeline (the gut → window → extract path)
  Stage 1: entity_resolver agent
    • prompt rule (entity_resolver/prompts/system.j2): "tokens matching
      datapod:<kind>:<id> are opaque — preserve verbatim, do not
      substitute, do not annotate"
    • output: "Here is a picture of me (Jukka) datapod:image:abc12345..."

  Stage 2: conversation_boundary agent
    • normal windowing — no special handling for pods

  Stage 3: window_critic + fact_extractor
    • PodInjector pre-hydrates the URI into a PodHeader block in the
      fact_extractor's prompt context — "datapod:image:abc... [image] (no caption yet)"
    • fact_extractor prompt has a "POD URI HANDLING" section with an
      edge-type vocabulary table by pod kind:
            image: depicted_in (default-safe), has_profile_image (canonical framing),
                   has_screenshot, has_receipt
            video: depicted_in, has_video, has_recording
            audio: mentioned_in, has_recording, has_voicemail
            email: mentioned_in, invoiced_via, replied_to
    • for "Here is a picture of (Jukka) datapod:image:abc..." emits:
            source: Jukka (entity, temp_id="entity_1")
            target: datapod:image:abc... (verbatim, treated as pre-resolved id)
            edge:   depicted_in       (default-safe — always emit)
            edge:   has_profile_image (intent-flavored — "picture of me" reads canonical)

  Stage 4: write_proposals
    • app/assistant/kg/proposal_writer.py pre-registers pod URIs in
      temp_to_uuid (POD_URI_RE check) before writing edges, so the
      edge writer doesn't drop them with "endpoint missing temp_id mapping"
    • claim_proposal + claim_proposal_node + claim_proposal_edge rows
      created; target_node_id on the edges = the pod URI literally

  Stage 5: proposal_promoter
    • _resolve_endpoint() accepts pod URIs as already-resolved kg_node ids
      (the kg_mirror Pod node already exists), bypassing proposal-node lookup
    • writes kg_edge_metadata rows:
            source_id=<jukka_kg_node_id>
            target_id=datapod:image:abc...
            relationship_type=depicted_in
            (and a second row for has_profile_image)
        │
        ▼
At rest, the KG now contains:
  • a Pod node (kg_node_metadata where node_type='Pod', category='image')
  • two edges from Jukka's entity node to the pod
  • the actual bytes at data/images/<hash[:2]>/<hash>.jpg
  • a sidecar at data/images/<hash[:2]>/<hash>.jpg.emipod.json
```

### What happens for video / audio / document uploads

The plumbing is the same shape; the only differences are:

- **Storage path**: `data/videos/<hash>/...`, `data/audio/<hash>/...` —
  same content-addressed convention as `data/images/`, different
  top-level dir per kind (see `data/images/README.md` for the flow
  path; same rules apply across kinds).
- **Pod kind**: `video`, `audio`, `pdf`, etc.
- **Metadata fields**: kind-specific (duration for audio/video, page
  count for pdf, etc.).
- **Edge vocabulary**: see the fact_extractor table — `has_video` for
  events anchoring a video, `has_recording` for audio, etc. The
  default-safe edges (`depicted_in` for video, `mentioned_in` for
  audio) match the image pattern.
- **Body extraction**: the searchable representation differs by kind
  (vision caption + OCR for image, transcript for audio, transcript +
  frame captions for video, OCR for pdf). All currently land in
  `pod.body` so `pod_search?query=...` works uniformly.

> **Status today (2026-04-29):** image is the only kind wired
> end-to-end through the chat upload path. Video and audio require
> (a) extending `image_ingest` into a kind-aware `media_ingest` (or
> sibling modules), (b) the corresponding storage directories under
> `data/`, and (c) a vision/transcription extraction pass to populate
> `pod.body`. The pipeline below it (kg_mirror, fact_extractor with
> the kind-keyed edge vocabulary, proposal_writer, promoter) already
> works for any pod kind — no changes needed.

## Phase 2: retrieval + action

User says: **"find the picture of me from today and email it to Katy."**

```
chat_gate (master_room) classifies the turn → routes via
chat_task_router_node → personal_admin_manager.

personal_admin_manager runs its agent loop. The planner reads the
"Image pods + email-with-attachments (find-and-send workflow)"
section of its system prompt and emits two tool calls in sequence:

Step 1 — locate the pod:
  pod_search(
    kind="image",
    linked_to_entity="Jukka",
    linked_via=["depicted_in", "has_profile_image"],
    since="today"
  )
        │
        ▼
app/assistant/pod_store/pod_store.py::PodStore.query
  • SQL subquery: target_id from kg_edge_metadata
        JOIN kg_node_metadata ON source_id = id
        WHERE label='Jukka' AND node_type='Entity'
        AND relationship_type IN ('depicted_in', 'has_profile_image')
  • combined with kind='image' and since='today' filters
  • returns one or more PodHeaders ordered by recency
        │
        ▼
The planner's response data.pods[*].pod_id list contains the matching
pod URIs. The planner picks one (typically the most recent or the
unique match) and proceeds.

Step 2 — resolve recipient and send:
  send_email(
    to="<katy@gmail.com>",   # resolved via existing get_email_thread / contacts path
    subject="Picture of me",
    body="Here you go.",
    pod_ids=["datapod:image:abc..."]
  )
        │
        ▼
app/assistant/lib/tools/send_email/send_email.py::SendEmail.execute
  • for each pod_id: PodStore.get(pod_id)
  • pod.metadata["stored_path"] (repo-relative) → absolute path
  • verifies file exists; aborts the whole send if any pod is missing
    or unbacked (no partial attaching)
        │
        ▼
app/assistant/lib/core_tools/email_tool/utils/gmail_api_client.py::GmailAPIClient.send_email
  • builds MIMEMultipart (text body + attachments)
  • for each attachment_path:
        - mimetypes.guess_type(path) → e.g. image/jpeg
        - read bytes, wrap in MIMEImage / MIMEAudio / MIMEBase as appropriate
        - Content-Disposition: attachment; filename="<basename>"
  • base64-encode the message and POST to Gmail's
    users().messages().send endpoint
        │
        ▼
Gmail delivers the email to Katy with the image attached. The tool
result reports message_id and attachments_sent count back up the
agent loop, which produces a chat reply confirming.
```

### Why no inlining of bytes

Throughout phase 2, the **bytes never leave their content-addressed
storage location until `MIMEImage` reads them at attach time.** Agents
pass `pod_id` strings between tool calls; PodInjector hydrates headers
(without the body) into prompts; only `send_email` actually opens the
file. This keeps:

- Chat history small (no base64 image dumps).
- Agent prompts cheap (PodHeader is ~100 chars; the image is ~MB).
- The loop reversible: any step can fail and be retried without
  re-uploading or re-deriving anything.
- Multi-attachment trivial: the planner can pass a list of pod_ids
  from a wider `pod_search` and `send_email` attaches all of them.

## What can go wrong (and what's safe-by-default)

| Failure | What happens |
|---|---|
| `ingest_image_file` fails (disk full, permission, etc.) | Falls back to legacy `[emi_image: <path>]` marker. Chat still works; no pod minted; no KG edges. |
| Sidecar write fails | Pod still minted, just unstamped. Reconciler can stamp later by content hash. |
| `entity_resolver` accidentally substitutes the URI | The fact_extractor's prompt rule treats `datapod:` tokens as opaque, so the URI still flows through to the edge target. Resolver substitution would only mangle a URI that already failed the pattern match, which is non-recoverable but visible in the resolved-message column. |
| `fact_extractor` misclassifies the intent | `depicted_in` (the default-safe) is always emitted — `has_profile_image` is the upgrade. Worst case: the photo is `depicted_in` only and `pod_search` with `linked_via=["depicted_in"]` still finds it. |
| `pod_search` returns 0 photos | The planner is instructed to widen `since` and retry; if still 0, ask the user. |
| `send_email` finds the pod but the file is gone | Refuses the whole send with a clear error rather than partial attachment. The reconciler then reports the dangling pod. |
| User uploads the same image twice | Content addressing dedupes — same sha256 → same pod_id → same kg_node row. Re-ingest is idempotent; existing edges aren't duplicated. |

## Where this lives in code

| Layer | Path |
|---|---|
| HTTP entry point | `app/routes/process_request.py` |
| Image storage + pod minting | `app/assistant/pod_store/image_ingest.py` |
| Pod store | `app/assistant/pod_store/pod_store.py` |
| KG mirror | `app/assistant/pod_store/kg_mirror.py` |
| File stamps | `app/assistant/pod_store/file_stamp.py` |
| Reconciler | `app/assistant/pod_store/image_reconcile.py` |
| Chat marker | `app/assistant/room_session_manager/services/surfaces/ui_inbound_service.py` |
| Resolver prompt | `app/assistant/agents/knowledge_graph_add/entity_resolver/prompts/system.j2` |
| fact_extractor prompt | `app/assistant/agents/knowledge_graph_add/fact_extractor/prompts/system.j2` |
| Pod-URI-aware edge writer | `app/assistant/kg/proposal_writer.py` |
| Pod-URI-aware promoter | `app/assistant/kg/proposal_promoter.py` |
| Pod search with entity filter | `app/assistant/lib/core_tools/pod_store/pod_store_tool.py`, `app/assistant/lib/tools/pod_search/` |
| Email-with-attachments | `app/assistant/lib/tools/send_email/send_email.py`, `app/assistant/lib/core_tools/email_tool/utils/gmail_api_client.py` |
| Find-and-send planner workflow | `app/assistant/agents/personal_admin/planner/prompts/system.j2` |
| Storage convention docs | `data/images/README.md`, `resources/identity/README.md` |
| Memos | `project_pod_body_vs_artifact_principle`, `project_image_storage_and_stamping` |

## Two-line summary

The image flows: `upload → image_ingest → PodStore.put → kg_mirror →
[chat continues, kg_pipeline runs in background] → fact_extractor
emits edges → promoter writes kg_edge rows`.

The retrieval flows: `chat turn → personal_admin planner → pod_search
(kind=image, linked_to_entity=user, since=today) → send_email
(pod_ids=[...]) → GmailAPIClient resolves to file → Gmail send`.
