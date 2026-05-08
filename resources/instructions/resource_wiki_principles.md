## Wiki principles

Wiki = projection of the KG. KG is truth. When wiki and KG disagree:
either the wiki is stale (refresh) OR the critic misread the schema
(dismiss with reason).

**Dirty detection** = bullet-text-diff vs `bullet_index` sidecar (NOT
`updated_at`). `description` updates and importance bumps don't change
bullets ⇒ no refresh.

**Gates.** Per-page cooldown (~1h), importance pre-filter, nano critic
gate. `refresh_wiki_page` may silently no-op — verify via sidecar.

**Common wiki-critic false positives** (all → dismiss):
- "X takes Y" + "X stopped Y" without checking `end_date` (closed era is rendered correctly)
- Date precision mismatch (`2024-03-15` vs "around 2024" = same fact)
- Source-relative phrases ("last year") without anchor — writer issue, not contradiction
- Two same-label States with different dates = two distinct eras

**Order:** mutate KG first, refresh second. Refresh before mutation
just re-renders the same wrong bullet. Don't blanket-refresh
neighbors — only entities whose bullets actually used the changed fact.
