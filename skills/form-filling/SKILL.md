---
name: form-filling
description: How to fill out web forms — identify fields, map values from the KG or from pods, fill them in via Playwright, leave the form ready for user review before submission. Covers the secret-field path that types pod values into the form without you ever seeing the value.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "fill out"
      - "fill in"
      - "fill the form"
      - "fill this form"
      - "complete the form"
      - "submit the form"
      - "application form"
      - "enrollment form"
      - "google form"
      - "registration form"
---

# Form filling skill

You've been asked to fill out a web form. This skill tells you how
to do it safely — especially for fields that need sensitive values
(SSN, DOB, account numbers) which you must never see.

## The protocol

1. **Open the form** — navigate the browser to the form URL via the
   standard `browser_navigate` MCP path.

2. **Scan the structure** — call `web_modal_scan` to get the list of
   actionable fields. The scan returns each field with a `ref`
   (accessibility-snapshot identifier), a label, an element type
   (textbox / combobox / checkbox / etc.), and surrounding context.

   **Vision fallback.** If the scan returns ambiguous or missing
   fields — bare `[element]` entries with no useful label, custom
   canvas-drawn controls, or fields that visibly exist on the page
   but don't appear in the snapshot — fall back to `web_page_coords`:
   pass a question like *"find the input labeled 'Date of birth' and
   return its coordinates"* and the tool will take a screenshot,
   reason about it visually, and return click/type coordinates. The
   critic also sees a screenshot each cycle (`playwright_critic_image`)
   and will call out cases where the planner's structural picture
   doesn't match the visual reality.

   The structural snapshot is your default — cheap, deterministic.
   Reach for vision when the snapshot fails you, not as a substitute
   for reading the snapshot first.

3. **Plan the field-map** — for EACH field, decide where the value
   comes from:

   | Field looks like | Source | How to fill |
   |---|---|---|
   | Name, email (yours or family), phone (you'll type publicly) | KG lookup or your own knowledge | `browser_type` with the literal value |
   | Address, city, state | KG lookup | `browser_type` with the literal value |
   | SSN, account number, full DOB, anything you suspect lives in a pod | **`web_type_secret`** | Pass `pod_id` + `projection`, never read the value |
   | Free-text comments / explanations | Your own composition | `browser_type` |
   | Radio buttons / checkboxes / dropdowns | KG fact or task instruction | `browser_click` |

4. **Fill sensitive fields with `web_type_secret`** — for each
   sensitive field:
   - Find the appropriate pod via `pod_search` or by knowing the pod
     id from context.
   - Call `web_type_secret(ref=<field_ref>, pod_id=<pod>, projection=<proj>)`.
   - The tool runs at courier authority, fetches the pod, types the
     value, returns "typed_length: N" — **no value in the result**.
   - DO NOT call `pod_fetch` to look up the value first and then
     `browser_type` with it. That defeats the privacy boundary and
     leaks the value into your transcript.

5. **Fill non-sensitive fields with `browser_type`** — straightforward.
   Use values you already know (your name, family member names from
   the KG, the user's email).

6. **Skip fields you don't know how to fill.** This is a hard rule.
   If a field's value isn't in:
     - the task description / additional information,
     - a clearly-applicable KG fact (Jukka's name, family member names,
       known contact info),
     - an explicitly-named pod (the task tells you "use pod X"),
   then **leave the field blank.** Do not guess. Do not fabricate. Do
   not infer "what would a reasonable value be" from the field label.

   Examples of fields to leave blank by default:
     - "Student ID Number" when the task didn't give one
     - "Parent/Emergency Contact phone" when the contact's phone isn't
       in the KG with high confidence
     - "Last 4 of SSN" when the task didn't authorize the SSN pod
     - Any field whose label you don't fully understand

   At the review step (next rule), list the fields you LEFT BLANK in
   your summary so the user can fill them in manually before
   submitting. The point is: a half-filled form the user can finish
   is better than a fully-filled form with fabricated values.

   Exception: required fields that block submission. If a required
   field is unknown, still skip it — but flag it explicitly in the
   review summary as "BLOCKING: field X requires a value I don't
   have." The user can either provide it or tell you to abandon the
   submission.

7. **Do NOT submit immediately.** Set `submit=False` on every fill.
   When all fields are filled, take a final snapshot via
   `web_modal_scan` (or `browser_snapshot`) and stop. Surface the
   filled form to the user via a ticket for review:

   ```
   create_dayflow_ticket(
     title="Review filled form: <form title>",
     message="I filled out <form>. Fields filled: <list>. Screenshot attached.",
     ticket_type="dayflow_orchestrator",
     ...
   )
   ```

   Wait for the ticket response. If user approves, submit via a
   click on the submit button (`browser_click` on the submit ref).
   If user rejects or asks for changes, follow the directive.

## Worked example

User: *"Fill out this Google Form with my info, leave the SSN field for me to confirm. https://forms.google.com/..."*

Steps:
1. `browser_navigate(url=...)`
2. `web_modal_scan()` → fields:
   - `ref=e1` label="Full name" (textbox)
   - `ref=e2` label="Email" (textbox)
   - `ref=e3` label="Social Security Number" (textbox)
   - `ref=e4` label="Date of birth" (date input)
   - `ref=e5` "Submit" (button)
3. Map:
   - e1: `browser_type(ref=e1, text="Jukka Virtanen")`
   - e2: `browser_type(ref=e2, text="semisimplemath@gmail.com")`
   - e3: `pod_search(label="SSN")` → `pod_id="datapod:identity.ssn:..."`
         → `web_type_secret(ref=e3, pod_id=..., projection="full")`
         The result says "typed_length: 11" — you never saw "123-45-6789".
   - e4: `pod_search(label="DOB")` → `web_type_secret(ref=e4, pod_id=..., projection="full")`
4. `browser_snapshot()` → screenshot
5. `create_dayflow_ticket(title="Review filled form", attach the
   screenshot)` and wait.
6. On approval: `browser_click(ref=e5)` to submit.

## Anti-patterns — don't do these

- **`pod_fetch(pod_id, projection="full")` then `browser_type(text=value)`**.
  You held the SSN in your transcript. Permanently. That's the failure
  mode the secret-pod design exists to prevent. ALWAYS use
  `web_type_secret` for fields whose value you shouldn't see.

- **Submitting without user review**. Even with allowlisted recipients
  on send_email, web forms are different — they often have one-shot
  side effects (account creation, enrollment commitment). Always
  stop at "form is filled, here's what it looks like, OK to submit?"

- **Guessing values for fields you don't know how to source**. If a
  field's value isn't in the KG, isn't in a pod, and isn't in the
  task description — ASK. Don't fabricate.

- **Reading `pod_fetch` results back into the prompt**. The pod store
  enforces this via authority gates, but it's still on you to avoid
  the call pattern. Use `web_type_secret` or pod_list (which returns
  only names and authority, not values).

## When this skill doesn't apply

- The task involves a PDF form, not a web form. (PDF form filling is
  a different surface — courier is `pdf_form_courier`, not Playwright.)
- The task is just to NAVIGATE a site or scrape content, no field
  filling. Use the standard Playwright tools without this skill.
- The form is already filled and the task is just to submit. You
  can do that directly with `browser_click` on the submit button.
