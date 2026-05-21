# Meal proposer house rules — Jukka's household

This file holds the household-specific calibration for meal_proposer:
worked examples, the recipe vocabulary this family actually cooks,
local restaurants worth considering, dietary constraints in detail.

Mirrors the noticer's house_rules pattern — separable from the agent's
stable scaffold so skill_distiller can append learned patterns here.

---

## Recipe vocabulary (recipes_house starter)

These are dishes the family cooks regularly. The proposer should prefer
these (novelty="familiar") for most meals.

- Salmon + roasted broccoli + rice
- Pasta carbonara
- Tacos (ground beef or chicken)
- Roast chicken + roasted veg
- Stir-fry (chicken + bell peppers + rice)
- Chili
- Breakfast burritos (eggs + bacon + cheese in tortilla)
- Overnight oats (portable breakfast)
- Grilled cheese + tomato soup

When the proposer references one of these, set `recipe_ref` to the
matching name. When it proposes a variation (e.g., "salmon + roasted
asparagus + rice" — swapped broccoli for asparagus), set
novelty="variation" and note the swap in `novelty_rationale`.

## Dietary constraints to respect (from KG, mirrored here for visibility)

- Jukka has GERD — avoid heavy tomato in evening meals
- Jukka is prediabetic — limit refined carbs, especially evening
- Jukka uses intermittent fasting — typically skip breakfast OR delay
  it to ~10am (check today's diet_log to see)
- Kids (Peter, Annika) are generally not picky but Annika has been
  skipping breakfast lately — portable options preferred for school
  mornings

## Worked example — meal proposal addressing the Jukka-fatigue concern

If the addressable_concerns list contains "Jukka's sleep/fatigue has
been recurring", a proposal that addresses it might look like:

```
actors: ["Jukka", "Katy", "Peter", "Annika"]
meal_window: "dinner"
date: "2026-05-20"
proposed_start_local: "2026-05-20T18:00:00-07:00"
dish: "Grilled chicken + roasted broccoli + brown rice"
source: "home_cooked"
primary_ingredients: ["chicken", "broccoli", "brown rice"]
needs_shopping: []
estimated_calories_per_person: 550
recipe_ref: "Roast chicken + roasted veg"
reasoning: "Lighter than usual + earlier serving time to support Jukka's
  sleep concern. Familiar recipe, all ingredients in inventory."
confidence: "high"
novelty: "familiar"
```

Notice:
- proposed_start_local is EARLIER than the default 18:30 family dinner
- dish is lighter than carbonara or tacos
- reasoning explicitly names the concern in prose ("supports the
  fatigue pattern" / "responds to Jukka's recurring tiredness")

## Family-roster awareness

If Katy is traveling, propose simpler solo meals for Jukka + leftovers
for the kids. If Jukka is solo (e.g., family on a weekend trip), propose
minimal one-person meals.

If date night is in `addressable_concerns` (from romantic_proposer's
intentions in the pod store), DON'T propose a family dinner for Jukka+Katy
that night. Propose a smaller family meal for Peter+Annika only.

## Local restaurants worth considering (when novelty="novel")

Phase 1c will add real web research for new places. For 1a, these are
known options:
- (TBD — add as we discover what Jukka and Katy enjoy)

When proposing a restaurant, set:
- source = "restaurant"
- recipe_ref = null
- needs_shopping = []
- novelty = "novel" or "familiar" depending on history
- estimated_total_cost_usd should be filled
