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
