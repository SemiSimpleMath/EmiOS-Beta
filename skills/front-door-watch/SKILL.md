---
name: front-door-watch
description: Per-camera context for the household's front-door Ring camera. Use when scene_analyzer runs on the front door feed — supplies what's normal at this view, who's expected, and what raises significance.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
---

# Front Door Camera

You are watching the front door of a private residence. The camera has a
view of the porch, the front walkway, and the street beyond.

## What's expected (NOT significant on its own)
- Family members coming and going during normal hours.
- Cars parked in the driveway or on the street.
- Foliage / shadows / weather causing motion (tree branches, wind, rain).
- Pets walking past on the sidewalk with their owners.
- Mail carriers and package couriers leaving items on the porch — note
  the delivery in the caption but only flag as significant if the package
  is large, the person lingers, or something looks off.

## What raises significance (importance 6+)
- An unfamiliar person at or near the door, especially after dark.
- Someone trying the door handle, peering into windows, or photographing
  the house.
- A package being TAKEN from the porch by someone other than a household
  member.
- A stranger remaining on the porch for more than the time a normal
  delivery takes.
- Vehicles stopping at the curb with occupants who don't get out.

## High alert (importance 8-9)
- Visible attempt to force entry.
- Multiple unfamiliar people gathered at the door at unusual hours.
- Anyone visibly armed.

## Notes
- The driveway is to the right of the front door from the camera's view.
- Children of the household sometimes play on the front steps in daylight —
  this is routine.
- Treat anything you cannot see clearly (dark frames, obstructed views)
  as low-importance and say so in the caption rather than guessing.

## Category rubric

After captioning, classify the frame into ONE category. Categories drive
downstream escalation — pick the closest match, not the most cautious one.
Use `unknown` ONLY when the frame genuinely doesn't fit any other
category (NOT as a hedge when you're unsure between two).

| Category | What it looks like |
|---|---|
| `food_delivery` | DoorDash / Uber Eats / Grubhub / Postmates driver, often with a thermal bag. Brief approach, drop, leave. |
| `package_delivery` | Amazon, UPS, FedEx, USPS, DHL driver. Carrying box(es). Brief porch interaction. |
| `household_out_with_dogs` | Recognizable family member leaving (or returning) with one or both dogs (Bonnie, Clyde). |
| `kids_leaving` | Peter or Annika departing — school morning, going to play, biking off. |
| `family_arriving` | Jukka, Katy, or kids returning home (no dogs leading). Driveway → door direction. |
| `car_by` | Vehicle passing on the street with no one approaching. No human action toward the house. |
| `wind` | Foliage/branches/leaves/shadows moving with no person or vehicle present. False-positive motion trigger. |
| `stranger_lingering` | Unfamiliar person on/near the porch for longer than a normal delivery, no obvious purpose. Between `unknown` and `emergency`. |
| `unknown` | Genuinely ambiguous: dark frame, partial view, person partly visible, frame too unclear to categorize. |
| `emergency` | Active attempt at forced entry, visible weapon, multiple unfamiliar people at unusual hours, anything urgently warranting attention. |

### Common-mistake notes

- A normal delivery driver lingering 5-10 seconds is NOT `stranger_lingering` — they're doing their job. Use the delivery category.
- Pets walking past on the sidewalk with their owners → `car_by` is wrong; if no movement toward the house at all and no person interacts with the door area, prefer `wind` (background motion) or `unknown` (genuinely uncertain).
- Mail truck driving past without stopping → `car_by`, not `package_delivery`.
- Family member returning with groceries → `family_arriving`, not `package_delivery`.
