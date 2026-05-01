# Pod skill — admin (act on pods you've been given)

You receive `pod_id` references in your task or in your inbound
message and you act on them — attach to email, fetch body to read,
unfurl content for downstream consumption. **You do not search for
pods yourself** (a finder agent did that and passed the ids forward).
If a task asks you to "find X and do Y", finding X is upstream — you
get the pod_id and do Y.

## When this skill applies

- The task includes a `datapod:<kind>:<id>` URI — directly in the
  task text, in the additional information, or hydrated as a
  PodHeader in your "Referenced pods" context block.
- The task says "send / email / attach / forward / unfurl / read" in
  combination with one of those URIs OR a media-flavored hint that
  upstream agents have populated pod_ids for.
- The user explicitly references "the picture I just shared" / "that
  receipt" / "that email" — and a pod_id is in the context to
  resolve which one.

If none of the above apply, ignore this skill.

## Pod kinds and how to handle each

Pods come in two structural shapes. Pick the right path BEFORE you call
any tool — passing a text-only pod to an attachment slot fails the whole
op (`send_email` aborts with "pods without backing files").

| Pod kind       | Has backing file? | Body content                                | Email handling                         |
|----------------|-------------------|---------------------------------------------|----------------------------------------|
| `image`        | yes (`stored_path`)| vision caption + OCR text                  | attach via `pod_ids=[...]`             |
| `video`        | yes               | caption / transcript                        | attach via `pod_ids=[...]`             |
| `email`        | **no**            | the email body text                         | inline body into email body or quote   |
| `chat_cluster` | **no**            | resolved chat transcript                    | inline body into email body or quote   |

The structural rule: `metadata.stored_path` exists → attachable as file.
`metadata.stored_path` absent → text-only, must be **inlined** into the
outgoing message body (or quoted, summarized, paraphrased — anything
that turns the pod's text into part of the message you're producing).

Mixed batches are fine: a request like "email me the photos plus the
chat threads" means call `send_email` once with `pod_ids=[<image_pods>]`
and `body` containing the rendered text of the chat_cluster pods.

## Email pods (preferred over Gmail tools for "find by sender / scan recent")

For tasks like "did I get an email from X today?", "what was that
invoice email about?", "show me recent emails from <sender>":

- Inbound emails are auto-minted as `kind=email` pods. Subject + sender
  + account + uid live in `pod.metadata`; body lives in `pod.body`.
- The preferred path is `pod_search(kind="email", query=<sender>,
  since=...)` because it doesn't hit Gmail.

Fall back to `get_email_*` tools (get_email_thread, get_important_emails,
get_email_messages) only when:
1. The email predates the pod system (older emails were never minted).
2. You need a fresh-from-Gmail mutation like `send_email`,
   `trash_emails`, mark-as-read, etc.
3. `pod_search` returns nothing for an obviously-recent message.

## Attaching pods to an email (file-backed kinds only)

`send_email` accepts `pod_ids` directly as attachments — but ONLY for
pods with `metadata.stored_path` set (image / video / audio / document):

```
send_email(
  to="<recipient_email>",
  subject="<subject>",
  body="<message text>",
  pod_ids=["datapod:image:abc...", "datapod:video:def..."]
)
```

The tool resolves each pod_id via `pod.metadata.stored_path` and
attaches with the original filename + inferred MIME type. If any pod
is missing or unbacked (no `stored_path`), the **whole send aborts** —
no partial attaching.

**Never put `chat_cluster` or `email` pod_ids in `pod_ids=[...]`.** They
have no backing file. Inline them instead (see next section).

**Never inline image bytes into chat or email body.** Always pass
image pod_ids and let `send_email` resolve at attach time. Keeps the
chat thread small and the loop reversible.

## Inlining pods into an email (text-only kinds)

For `chat_cluster`, `email`, or any other text-only kind:

1. `pod_fetch(pod_ids=[...])` to get full pod records (header + body +
   metadata).
2. Format the bodies into the email body as labeled blocks. Example:

   ```
   Here are the food-related chat clusters from today:

   --- Chat thread 1 (10:14 AM) ---
   <pod 1 body>

   --- Chat thread 2 (12:45 PM) ---
   <pod 2 body>
   ```

3. Call `send_email` WITHOUT `pod_ids`. The body carries the content.

If the user asks "email me X" and X is text-only pods, this is the
right pattern. Don't try to attach them — the tool will refuse and
your loop will graceful-exit at cycle 0.

## Unfurling pods (reading content for the user)

If the user asks you to summarize, paraphrase, or report on a pod's
content, you'll need the body. `pod_fetch` takes a list of pod_ids
and returns full pod records (header + body + source_refs +
metadata).

```
pod_fetch(pod_ids=["datapod:email:abc..."])
```

Then quote / summarize / extract from `body` as the task requires.
The body text for image pods is the vision caption + OCR; for email
pods, the email body; for chat_cluster, the resolved transcript.

## Resolving recipients

If the user names a person ("email it to Katy"), look up Katy's
canonical email address before calling send_email — `get_email_thread`
with `participant_email`, contacts lookup, or the entity card for
Katy if it carries `email` in attributes. Don't guess.

## Hydrated pod headers in your context

If the inbound message or task contains `datapod:<kind>:<id>` URIs,
your context already includes a "Referenced pods" block with their
headers (no body). Read the headers first — they tell you what each
pod is about and whether `pod_fetch` is needed. Most actions don't
need the body; only quoting / summarizing / extracting / inlining
does. Crucially, the header tells you the **kind**, which decides
whether the pod attaches as a file or inlines as text.
