# Household food registry — Jukka's family

The canonical list of what this household actually eats. Every weekly
meal plan should LEAN HEAVILY on this list — these are the dishes,
fast food, takeout, and restaurants the family knows and accepts.

The planner can occasionally propose something new (novelty slot,
~1 per week max) but the bulk of every week comes from here.

This file is hand-edited by Jukka. Future work: a "Promote to baseline"
button on `/meals` that adds an accepted novelty dish/venue here after
positive feedback.

---

## 1. Home-cooked dishes

Jukka does the cooking. This is the COMPLETE list of dishes he makes
— the planner should pick `home_cook` slots from here ONLY. If a
desired dish isn't on this list, use `slot_type=novelty` instead.

- Salmon
- Fettuccine alfredo with meatballs *(needs a Trader Joe's run — TJ
  trip every 2-4 weeks; buy enough to make this dish 2 times per trip)*
- Hot dogs
- Pork chops
- Farfalle with sausage and alfredo sauce
- Fish sticks
- Sausages
- Orange chicken
- Fish and chips
- Frozen pizza
- Lasagna (heat store-bought)
- Noodle casserole *(rarely)*

### Comfort food subset (always reliable)

The home-cook items that always work — pick these on fatigue weeks,
busy weeks, or when kids are picky.

- Salmon
- Fettuccine alfredo with meatballs
- Pork chops
- Farfalle with sausage and alfredo sauce
- Frozen pizza
- Hot dogs (kids love)

### Friday Night Meats is NOT food

"Friday Night Meats" (FNM) is a recurring family/friends Zoom social
that happens every Friday 8-10pm. The misspelled "Meats" is a Bob's
Burgers reference. It has nothing to do with what's for dinner. The
planner should NOT treat FNM as a meal slot, an anchor, or a dish.
Friday dinner is independently planned — usually pizza, per section 5.

### Notes on cook variety

- Jukka cooks all of the above; Katy generally doesn't cook dinner.
- "Fettuccine alfredo with meatballs" and "Farfalle with sausage and
  alfredo sauce" share ingredients but are distinct dishes — don't
  treat them as duplicates.
- "Frozen pizza" and "Lasagna (heat store-bought)" are heat-up,
  not real cooks — easy weeknight fallbacks.

---

## 2. Fast food

- Wendy's
- McDonald's (sometimes breakfast)

---

## 3. Takeout / delivery

Sit-down restaurant pickup AND fast-food-chain pickup both live here —
the household orders from all of these rather than dining in. The
planner uses `slot_type=takeout` for everything in this section.

- **China Palace** — the big one. When ordered, usually covers 2-3
  days of leftovers. If China Palace is on the plan, the following
  1-2 dinners should be `leftover` from it.
- Islands (burgers, occasionally)
- The Melt
- Rubio's
- Panda Express *(~once a month — a real cadence pin, not just
  "occasionally")*
- **In-N-Out** *(favorite)*
- **Chick-fil-A** *(favorite)*
- **Del Taco** *(favorite)*
- **Taco Bell** *(favorite)*
- Carl's Jr *(sometimes)*
- Insomnia Cookies *(dessert / treat, not a main meal — don't
  schedule as a dinner slot)*
- Jamba Juice *(drink/snack — not a meal slot)*
- Starbucks *(drink/snack — not a meal slot)*

---

## 4. Dine-out restaurants

- Sushi place *(Jukka loves it — good Friday or weekend dinner pick)*
- Thai dinner place *(Katy-friendly thai — works for family dinner)*
- Thai Spice *(Jukka's lunch place — different from the family thai
  dinner spot)*
- **Corner Bakery** *(Saturday or Sunday breakfast — established
  weekend pattern, ~1 per weekend typical)*
- Olive Garden
- Mimi's (occasionally)
- The Poached Kitchen *(takeout-ish but high-end; reserve for special
  occasions)*
- Ruth's Chris *(rare — special occasions only; usually pickup)*

---

## 5. Weekly patterns (recurring)

These are the rhythms the planner should respect WITHOUT being told
each week:

- **Friday dinner: pizza** — typically frozen pizza at home OR pizza
  delivery. Planner picks based on the week (delivery if Friday is
  busy, frozen if a quieter at-home night).
- **Weekend breakfast: Corner Bakery** — Saturday OR Sunday, ~once
  per weekend. Use `slot_type=dine_out` with `dish="Corner Bakery"`.
- **Eat-out cadence: 2-3 times per week** total across fast_food +
  takeout + dine_out. This household genuinely likes eating out and
  the planner should reflect that — DON'T undershoot.
- **China Palace cascade**: a China Palace order produces 2-3 days
  of leftovers. Schedule the leftover slots explicitly.
- **TJ supply window**: the fettuccine-alfredo-with-meatballs dish
  needs a Trader Joe's trip. TJ runs happen every 2-4 weeks and stock
  enough for 2 batches of the dish. Don't schedule fettuccine alfredo
  unless TJ ingredients are reasonably current.

---

## 6. What NOT to propose

Things tried and rejected, OR outside the household's diet patterns.

- Onions in any quantity for Katy
- Zucchini for the kids (Peter + Annika won't eat it)
- Heavy egg-based dishes for the kids (only Jukka + Katy like eggs)
- Tomato-heavy evening dishes for Jukka (GERD)
- Complex weekday breakfasts (no time to cook on weekdays — flex it)
- Inventing a home-cooked dish that's not on the section-1 list
  (use `novelty` instead and own that it's experimental)
- Naming a restaurant that's not on sections 2-4 (use `novelty` for
  trying a new place)

---

## 7. Health + variety notes

- **Spread eating-out across the week.** 2-3 ordering-out slots per
  week is the target; don't bunch them on consecutive nights.
- **Avoid repeating any specific dish or venue within 14 days** when
  possible. If "Pork chops" was last Tuesday, pick something else
  this week.
- **Healthier nudge when warranted.** If the past 4 weeks show 12+
  fast-food/takeout/dine-out slots (well above the ~10 target), bias
  this week to the home-cook end and call it out in the theme.
- **Calorie restriction** for Jukka is active but he's NOT
  intermittent fasting right now — breakfasts are still flex (cereal
  etc.), just portion-aware on dinners.
- **Fatigue pattern**: when concerns flag fatigue, lean into the
  comfort-food subset (section 1) — these are the no-friction picks.
- **Kids' protein**: even on lighter dinner nights, make sure Peter
  and Annika get protein. Hot dogs, sausages, fish sticks, orange
  chicken are easy wins here.
