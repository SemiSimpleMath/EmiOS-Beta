---
name: critic-handling
description: How to treat CRITIC RESULT entries from a critic agent — when must_revise_plan is true, follow critic_actionable_change unless explicit override is justified. Use whenever a planner has a critic wired into its manager's flow.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  applies_when: "planner has a critic wired (critic_post_node in the manager flow)"
---

## Critic
On `CRITIC RESULT` with `must_revise_plan: true`: follow `critic_actionable_change`. To choose otherwise, name your reason in `what_i_am_thinking`. Two similar critic diagnoses in a row = you are looping; follow the prescribed change.
