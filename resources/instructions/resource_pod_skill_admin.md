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

## Attaching pods to an email

`send_email` accepts `pod_ids` directly as attachments:

```
send_email(
  to="<recipient_email>",
  subject="<subject>",
  body="<message text>",
  pod_ids=["datapod:image:abc...", "datapod:video:def..."]
)
```

The tool resolves each pod_id to its backing file via
`pod.metadata.stored_path` and attaches with the original filename
and inferred MIME type. Image / video / audio / document pods all
work. If any pod is missing or unbacked, the **whole send aborts** —
no partial attaching. So validate pod existence before promising to
send.

**Never inline image bytes into chat or email body.** Always pass
pod_ids and let `send_email` resolve at attach time. Keeps the chat
thread small and the loop reversible.

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
need the body; only quoting / summarizing / extracting does.
