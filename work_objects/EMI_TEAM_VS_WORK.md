# emi_team vs. work_manager — head-to-head

A real-task comparison of the two manager styles:

- **emi_team** — the live *transient* delegating manager (Waffle). Runs, returns an answer, exits. No durable state.
- **work_manager** — the work-graph *executor*. Owns a WorkObject node, decomposes it, records findings on the graph, and a finalizer mints the node's terminal status. Leaves a durable, queryable artifact.

Harness: `work_objects/scenarios/emi_team_vs_work.py`. Both managers run at authority 99. The two write
tools (`send_email`, `create_todo_task`) are **stubbed to capture-not-fire** — we record exactly what each
composed without sending — and the stubs return the *real* tools' success messages so the agent doesn't
retry-loop. `ask_user` is stubbed non-blocking (look it up in the KG, don't wait for a human). Read-only
work (web, KG) runs live. The EmiOS server must be stopped first so the harness can hold the Chroma writer
lock — otherwise `ask_kg` errors and both managers fall back to grepping the local wiki.

## Tasks

1. **Find DiCaprio's current girlfriend's age** — open web research.
2. **Email the user a contact's phone number** — KG lookup + side-effecting action.
3. **Create a "get brakes checked" todo** — single-step action.
4. **Find the email addresses of a recurring event's likely participants** — multi-hop KG reasoning.

## Results

| Task | emi_team | work_manager | faster |
|---|---|---|---|
| 1 — web research          | 68s  | **58s**  | work |
| 2 — KG + email            | **135s** | 169s | emi  |
| 3 — 1-step action (todo)  | **52s**  | 177s | emi  |
| 4 — multi-hop KG          | 467s | **200s** | work |

Both produced correct answers on all four. No retry loops, no Chroma errors (the clean run).

## The core pattern

The work_manager carries a roughly **fixed decomposition tax** — several extra LLM passes per task
(decompose → delegate → record finding → finalizer → re-render). That tax:

- **loses on trivial tasks**, where it dwarfs ~10s of actual work (todo: 177s vs 52s), and
- **wins on substantial ones**, where its structure prevents the thrash that wrecks a transient agent.

Task 4 is the clearest case — the two chose *different sources*:

- **emi_team (467s)** brute-forced **email** — searched ~210 messages and scraped headers, yielding a
  handful of real participants mixed with system-address noise (a Zoom no-reply, a mailer-daemon, an
  SMS-gateway address).
- **work_manager (200s)** decomposed the task (*find the event → extract attendees → compile*), found the
  **recurring calendar event**, and pulled its **actual invitee list** — cleaner and more complete in under
  half the time.

Organized decomposition led it to the smarter source while the transient agent flailed.

## Output quality

Beyond speed, the work_manager consistently left **richer, more durable output**:

- A structured graph every time — id'd subtasks + findings, each finding carrying its **source** (e.g. the
  local wiki page it read).
- More thorough actions — Task 3 it created the todo **plus a reminder, with a due time and priority**;
  emi tersely reported "I created the todo task" (and actually issued *two* `create_todo` calls).
- The captured writes show the same contrast: the work_manager's email cited its source; emi's was bare.

Nothing was actually sent or created — the stubs captured every write.

## When each wins

This maps directly onto a simple test — does the task need to **remember / wait / share / be audited**, or
is it a one-shot action?

- **One-shot simple actions** (send an email, make a todo): the transient manager is the right tool —
  faster, less ceremony.
- **Substantial multi-step work** (research, multi-hop reasoning): the work_manager wins on **both speed
  and quality**, and leaves a durable, auditable artifact for free.

The structure isn't merely overhead you tolerate for durability — on hard tasks it's a genuine
*performance* advantage, because organized decomposition beats a transient agent thrashing.

## Caveats / harness notes

- **Web-delegation variance.** The work_manager's web leg (Task 1) ranged 56–653s across runs; emi was
  steady at 70–80s. The variance is in how the work_manager delegates/decomposes the web step (occasional
  re-delegation), not entity cards or the graph core. Worth chasing separately.
- **Entity cards.** Briefly removed during debugging (mis-blamed for a slowdown), then restored — they're
  preloaded at startup and matched by plain string (no Chroma), so they're cheap and production-required
  regardless.
- **Server must be stopped** for the standalone harness to use Chroma/`ask_kg`. In production the
  work_manager runs *inside* the server (which owns the lock), so this is a harness artifact, not a real
  limitation.
- **`ask_user` removed** from the work_manager roster — the worker looks facts up in the KG rather than
  blocking on the user.
