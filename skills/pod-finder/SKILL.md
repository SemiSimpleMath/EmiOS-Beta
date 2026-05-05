---
name: pod-finder
description: How to search for pods (`pod_search`, `kg_query` escalation) and pass `pod_id` references forward to a downstream agent. The finder does NOT act on pods — searching only. Use when the task asks to find / locate / list a media artifact (picture, photo, image, video, audio, document, email, conversation, receipt, invoice) or references a `datapod:<kind>:<id>` URI.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  auto_inject_when:
    task_keywords: ["pod", "datapod", "picture", "pic", "photo", "image", "video", "recording", "audio", "receipt", "invoice", "conversation"]
---

# Pod skill — finder

You search for pods and pass `pod_id` references forward to whichever
agent will act on them. **You do not act on pods yourself** (sending
email with attachments, persisting state, etc. — that's a different
agent's job). You return pod URIs in your output and downstream agents
take it from there.

## When this skill applies

- The task asks to find / locate / look up / show / list a picture,
  photo, image, video, audio, document, email, conversation, receipt,
  invoice, or any other media artifact **and the answer plausibly lives
  in stored memory** (something the user mentioned, sent, or worked with
  before).
- The task references "pods" or contains a `datapod:<kind>:<id>` URI.

If none of the above apply, ignore this skill.

## When NOT to use this skill — call bash_manager instead

The pod store is **stored memory**, not a filesystem index. If the task
is anchored on a local filesystem location, the source of truth is the
disk, not the pod store. Route to `bash_manager` instead:

- "find any picture in my Pictures folder" / "from `~/Pictures`"
- "the doc in `~/Documents/...`"
- any task that names a real path (`~/X`, `C:\Users\...\X`)
- "send me a [file kind] from my [folder name]" — file is on disk

`bash_manager` lists the directory, picks the file, mints a pod via
`mint_pod_from_path`, and returns the pod_id. That pod_id is what you
pass forward (e.g. to `send_email`). Never `pod_search` for files in a
named filesystem location — pods only exist for files that have already
been minted, and most filesystem files have not.

## Two retrieval surfaces

| Tool | Sweet spot | Avoid for |
|---|---|---|
| `pod_search` | Indexed, single-hop, opinionated. Filters: `kind`, `tags`, `since`, `query` (substring on body+one_liner), `linked_to_entity` (single-hop KG join), `linked_via` (edge-type list). Returns hydrated `PodHeader` objects. **Default tool** — start here. | Multi-hop graph walks, edge-type-precise structural queries. |
| `kg_query` | Read-only SQL over `kg_node_metadata` + `kg_edge_metadata`. Pods are KG citizens (every pod has a `node_type='Pod'` row mirrored automatically), so you can write `WHERE source_id IN (SELECT id FROM kg_node_metadata WHERE label='Peter') AND relationship_type='participant'` -style joins. | Anything `pod_search` already covers — kg_query is verbose to write and easy to compose wrong. |

**Fallthrough rule:** try `pod_search` first. If the question requires
multi-hop traversal (e.g., "photos at events Peter attended" needs
Event → participant → has_photo, two hops), fall through to `kg_query`.

## pod_search filter compositions

```
"the picture of me from today"
  → pod_search(kind="image",
               linked_to_entity="<primary_user_label>",
               linked_via=["depicted_in", "has_profile_image"],
               since="today")

"my profile picture"
  → pod_search(kind="image",
               linked_to_entity="<primary_user_label>",
               linked_via=["has_profile_image"])

"emails from Acme this week"
  → pod_search(kind="email", query="Acme", since="7d")

"recent invoice emails"
  → pod_search(kind="email", query="invoice", since="2w")

"video of Peter's birthday"
  → pod_search(kind="video", linked_to_entity="Peter", query="birthday")

"food pods since Tuesday"
  → pod_search(tags=["food"], since="<date>")

"that conversation about creamer"
  → pod_search(kind="chat_cluster", query="creamer")
```

## kg_query escalations

```
"photos at events Peter attended"
  → kg_query("""
      SELECT n.id AS pod_id, n.label
      FROM kg_node_metadata n
      JOIN kg_edge_metadata photo_e
        ON photo_e.target_id = n.id
       AND photo_e.relationship_type IN ('has_photo','has_video')
      JOIN kg_edge_metadata participant_e
        ON participant_e.source_id = photo_e.source_id
       AND participant_e.relationship_type = 'participant'
      JOIN kg_node_metadata p
        ON p.id = participant_e.target_id
      WHERE n.node_type = 'Pod' AND p.label = 'Peter'
    """)
```

## Return discipline

When you find pods:

- Emit each `pod_id` verbatim in your output text. The downstream agent
  + the PodInjector will hydrate headers from them automatically.
- **Do NOT call `pod_fetch` to read bodies unless the question requires
  body content to answer.** Most "find" tasks end with "here are the
  candidates" + pod_ids — the next agent decides whether to open them.
- If the search returns 0 pods, widen filters once (drop tags / extend
  `since` / loosen `query`) and retry before reporting empty.

## Hydrated pod headers in your context

If the inbound message contains `datapod:<kind>:<id>` URIs, your
context already includes a "Referenced pods" block with their headers
(no body). Read the headers — they tell you what each pod is about
and what kind it is. You may decide a search is unnecessary because
the answer is in the headers already.
