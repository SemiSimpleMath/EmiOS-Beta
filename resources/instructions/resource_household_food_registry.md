# Household food registry — Jukka's family

The canonical list of what this household actually eats. Every weekly
meal plan should LEAN HEAVILY on this list — these are the dishes,
fast food, takeout, and restaurants the family knows and accepts.

The planner can occasionally propose something new (novel_try slot,
~1 per week max) but the bulk of every week comes from here.

This file grows by hand for now — Jukka edits it directly. Future
work: a "Promote to baseline" button on `/meals` that adds an
accepted dish to one of these sections after it gets positive feedback.

---

## 1. Home-cooked dishes (the baseline)

Dishes the family cooks regularly at home. The planner picks dinners
and weekend lunches from here unless there's a specific reason to
deviate.

### Comfort food (the always-works subset — high reliability)

These are the dishes that everyone in the household accepts without
complaint. Use these when the week needs an easy win — fatigue, busy
day, kids being picky.

- Friday Night Meats — burgers (the recurring Friday tradition)
- Roast chicken with carrots and potatoes
- Pasta carbonara
- Tacos (ground beef or chicken)
- Stir-fry (chicken + bell peppers + rice)
- Chicken tenders with oven potatoes
- Grilled cheese with tomato soup

### Regular rotation (good but slightly less universal)

- Lemon-garlic salmon with broccoli and rice
- Sheet-pan chicken with roasted vegetables
- Chili
- Pasta with marinara
- Sushi-style rice bowls (at home)
- Ham fried rice with carrots
- Cheese omelets
- Breakfast burritos (eggs + bacon + cheese in tortilla)

### Lunch staples

Weekday lunches are usually leftovers from the night before, but when
a fresh lunch is needed:

- Ham-and-avocado sandwiches with cucumber
- Brie-avocado-cucumber sourdough sandwiches
- Bagels with cream cheese and berries
- Turkey-avocado wrap
- (Add more as you confirm what lands.)

### Breakfast (when "planned", not flex)

Most mornings are flex (cereal, fruit, yogurt). When a planned
breakfast lands:

- Sunday pancakes
- Overnight oats
- Eggs + toast

---

## 2. Fast food we do

The places the household actually orders from when ordering out. The
planner uses `slot_type=fast_food` and puts the venue here.

CADENCE: at most ~1 fast-food slot per week, less if the past 4 weeks
show a rising trend. The planner reads `recent_planned_meals` to
gauge frequency.

- (TBD — Jukka to fill in: In-N-Out? Chipotle? McDonald's? Habit
  Burger? etc.)
- (TBD — preferred order at each one if helpful)

---

## 3. Takeout / delivery

Restaurants the household orders pickup or delivery from. Distinct
from "dine out" — these are nights the food comes home.

CADENCE: ~1-2 per month typically.

- (TBD — Jukka to fill in)

---

## 4. Dine-out restaurants

Restaurants the household actually visits (sit-down meals). The
planner uses `slot_type=dine_out` when it proposes one.

CADENCE: ~1-2 per month typically, more if it's a special occasion.

- (TBD — Jukka to fill in: local favorites, Friday Night Meats venue
  if it's ever out, anniversary spots, etc.)

---

## 5. What NOT to propose

Things that have been tried and rejected, OR that fall outside the
household's diet patterns. Add to this list as we learn from feedback.

- Onions in any quantity for Katy
- Zucchini for the kids (Peter + Annika won't eat it)
- Heavy egg-based dishes for the kids (only Jukka + Katy like eggs)
- Tomato-heavy evening dishes for Jukka (GERD)
- Complex weekday breakfasts (no time to cook on weekdays)
- (Add more as we observe what doesn't work.)

---

## 6. Notes on variety + health

- Spread fast food + takeout + dine_out across the month — don't bunch.
- Avoid repeating any specific dish within 14 days when possible.
- When the recent_planned_meals window shows lots of fast food /
  takeout, the planner should bias toward healthier home-cook options
  this week AND can call this out in the week_theme.
- Jukka is calorie-restricting (not actively IF right now). Bias
  dinners lighter when possible without making them boring.
- Jukka has a fatigue pattern — easier weeknight cooks help.
- Kids are growing — protein + veg is fine even when dinners are light.
