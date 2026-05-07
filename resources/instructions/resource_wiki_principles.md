## Wiki principles — derived from KG

Wiki pages render facts that already exist as KG nodes/edges. The KG
is truth; the wiki is a projection. When `wiki_consistency_critic`
flags "wiki says X but KG says Y", the answer is one of:

1. KG actually says Y (verify by reading the underlying nodes) → wiki is stale → refresh.
2. KG doesn't say Y → critic is misreading the schema → dismiss with reason.

### Pipeline

`page_writer` reads an entity's neighborhood, drafts the page, writes
both `wiki/<entity>.md` and a `bullet_index` sidecar. Sidecar is the
diff baseline.

### Dirty detection — bullet-text-diff, not updated_at

A section is dirty iff rendered bullets produce different text than
the sidecar's last text. Importance bumps and `description` updates
on hub nodes do NOT trigger refresh — bullets don't change, section
isn't dirty.

### Cooldown + importance gates

- Per-page cooldown (~1h) absorbs repeat triggers.
- Importance pre-filter: only entities ≥ threshold get a page.
- Nano critic gate: cheap pre-rewrite check skips no-op refreshes.

`refresh_wiki_page` may silently no-op when these gates apply. Verify
via the bullet_index sidecar timestamp.

### Common wiki critic false positives

- **Present-tense vs end_date**: critic flags "X takes Y" + "X stopped Y" without reading the State's `end_date`. If the State is closed, both are correct — wiki rendering a closed era as "X takes Y" is the present-tense canonical form. Dismiss.
- **Date precision mismatch**: KG `start_date=2024-03-15`, wiki "around 2024" — same fact, different precision. Dismiss.
- **Source-relative phrases**: wiki preserves "last year" / "next month" without anchor — usually a writer-side rendering issue, not a real contradiction.
- **Non-overlapping eras read as duplicates**: two `Pregnancy` States with different dates = two pregnancies. Dismiss.

### Wiki vs KG — which is the issue

- **State open in KG, prose says ended** → KG is wrong. Close the State, then `refresh_wiki_page`.
- **State already closed in KG, wiki still renders open** → wiki is stale. `refresh_wiki_page` only.
- **Mutation order**: KG first, regen second. Refreshing before mutation produces the same wrong render.

### Don't refresh adjacent pages without cause

Closing Annika's `takes_art_lessons` State affects Annika's page.
Don't blanket-refresh Broadway Arts unless its bullets actually
referenced that fact.
