---
name: whole-house-lights
description: How to turn the whole-house lights (or one room / one light) on or off reliably via the lights_control tool — ONE set_light_power call with no selector targets every light. Use whenever a task is about turning lights on/off across the house.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  applies_when: "controlling home lights through lights_control (Kasa/TP-Link)"
  task_keywords:
    - "lights"
    - "light"
    - "lamp"
    - "whole-house lights"
    - "whole house lights"
    - "all lights"
    - "house lights"
    - "turn on the lights"
    - "turn off the lights"
    - "lights on"
    - "lights off"
---

## Controlling lights

`lights_control` is the ONLY light primitive — use it; do not improvise across tools.

**Whole house (the common case):** ONE call —
`lights_control(command="set_light_power", state="on" | "off")` with **NO `room` and NO `light_id`**.
Omitting both selectors targets *every* light. Do **not** pass `"all"` as a room. Do **not** issue
per-light or repeated commands to cover the house.

**One room:** add `room` (a substring of the device alias, e.g. `"Bedroom"`, `"Living Room"`).
**One light:** add `light_id`.
**Dim / color:** `set_light_brightness` (0–100) / `set_light_color` (e.g. `"warm white"`).

## Do not

- Do **not** keep trying alternate strategies. One successful `set_light_power` IS the whole job — if it
  reports the lights are already on/off, you are **done**. Stop.
- If you genuinely don't know what lights/rooms exist, call `list_lights` **once** first, then act.
