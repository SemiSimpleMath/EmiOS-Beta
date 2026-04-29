# Pod skill — find, pass, attach

A pod is a URI-addressable content unit (chat transcript, email,
image, video, audio, document). Pods are referenced as
`datapod:<kind>:<id>` strings. **Pass pod_ids between agents and tools;
fetch the body only when you actually need to read it.**

## When this skill applies

- The task or information mentions a picture, photo, image, video,
  audio, document, attachment, or recording.
- The task says "find" / "find me" / "show me" / "send" / "email"
  in combination with one of the above.
- The message you received already contains a `datapod:<kind>:<id>`
  reference (PodInjector has already hydrated headers for these into
  your context — read them).

If none of the above apply, ignore this skill — pods aren't relevant
to the current task.

## Email pods (preferred for "find by sender / scan recent" tasks)

- Every inbound email is auto-minted as a `kind=email` pod in
  pod_store. Subject + sender + account + uid live in pod metadata;
  the body lives in the pod and is read via `pod_fetch` only when
  needed.
- For tasks like "did I get an email from X today?", "what was that
  invoice email about?", "show me recent emails from <sender>", the
  preferred path is `pod_search` with `kind=email` + sender / scope /
  since filters. This is faster and cheaper than `get_important_emails`
  / `get_email_messages` because it doesn't hit Gmail.
- Workflow: `pod_search` returns headers (one_liner = "Sender: Subject")
  + pod_ids. **Pass the pod_ids forward in your output text, do NOT
  inline the bodies.** A downstream agent or the user-facing renderer
  will redeem the body via `pod_fetch` when it actually needs the text.
- Fall back to `get_email_*` tools only when (a) the email predates the
  pod system (older emails were never minted), (b) you need a
  fresh-from-Gmail action like `send_email` / `trash_emails`, or (c)
  pod_search returns nothing for an obviously-recent message.

## Image pods + email-with-attachments (find-and-send workflow)

- User-attached images are minted as `kind=image` pods. KG edges from
  the user (e.g. `Jukka --depicted_in--> datapod:image:abc...`) record
  who's in each image, who owns it, who shared it.
- For "find the picture of me from today and email it to Katy"-style
  tasks, the workflow is two tool calls in sequence:

    Step 1 — locate the image pod(s):
      pod_search(
        kind="image",
        linked_to_entity="<primary_user_label>",
        linked_via=["depicted_in", "has_profile_image"],
        since="today"      # or appropriate window
      )
      → returns headers; capture the pod_id(s) from data.pods[*].pod_id

    Step 2 — send the email with attachments:
      send_email(
        to="<recipient_email>",
        subject="<subject>",
        body="<message text>",
        pod_ids=["datapod:image:abc..."]   # the pod_ids from step 1
      )
      → send_email reads each pod's metadata.stored_path and attaches
        the backing file. Image / video / audio / document pods all work.

- Resolving the recipient: if the user names a person ("email it to
  Katy"), use `get_email_thread` with `participant_email` lookup OR
  consult contacts to find the canonical email address before calling
  send_email.
- Never inline image bytes into chat. Always pass pod_ids and let
  send_email resolve the file at attach time. This keeps the chat
  thread small and the loop reversible.
- If pod_search returns 0 image pods for the requested window, retry
  with a wider `since` before giving up. If still 0, ask the user via
  ask_user before sending.

## Hydrated pod headers in your context

If a `datapod:<kind>:<id>` URI appeared in the inbound message, your
context already includes a "Referenced pods" block with the headers
(pod_id, kind, tags, one_liner, scope_id, created_by, created_at,
content_type) — no body. Read the headers to decide whether to act
on the pod directly or call `pod_fetch` to read its body.
