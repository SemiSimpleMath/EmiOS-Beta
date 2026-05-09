---
name: extending-emi-cameras
description: How to add a new camera to EmiOS. Cameras are declarative entries in configs/cameras.json — the dispatcher routes each frame by camera_id to the configured analyzer + storage + pod policy. Use when the task involves adding a camera, snapshot source, or vision-analysis pipeline.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new camera"
      - "add camera"
      - "snapshot source"
      - "ring camera"
      - "rtsp camera"
      - "camera analyzer"
      - "extend emi cameras"
---

# Adding a new camera

Cameras are declarative entries in `configs/cameras.json`. The
`camera_dispatcher` (wired as the `camera_dispatch` routine) reads
the registry on startup and routes every snapshot event by
`camera_id` to the right analyzer + storage + pod policy.

## Camera registry entry

```json
{
  "id": "<camera-id-from-source>",
  "name": "Garage",
  "alias": "Garage",
  "storage_folder": "data/cameras/garage",
  "analyzer": "garage_analyzer",
  "trigger": {
    "type": "external_event",
    "event_topic": "ring_snapshot_captured",
    "match_kind": "garage"
  },
  "post_handlers": [],
  "pod_policy": {
    "conditions": [
      { "field": "is_significant", "equals": true }
    ],
    "source_kind": "ring_garage_significant",
    "one_liner_field": "caption",
    "body_field": "caption"
  }
}
```

## Field reference

- `id` — the stable identifier the trigger event will carry as
  `camera_id` in its payload. For Ring this is the Ring camera_id;
  for local RTSP cameras it can be any unique string.
- `storage_folder` — relative to repo root. Frames land in
  `<folder>/YYYY-MM-DD/HHMMSS.jpg` plus `.txt` (human sidecar) and
  `.meta.json` (structured analyzer output).
- `analyzer` — the agent name that runs on each frame. See
  `extending-emi-agents` to add a new analyzer.
- `trigger.type` — `external_event` is the only type today.
- `trigger.event_topic` — the event_hub topic this camera fires on.
- `trigger.match_kind` — fallback identifier for events that don't
  carry the original camera_id (e.g. local RTSP captures tagged
  `kind: "garage"`).
- `post_handlers` — list of named functions in
  `app/assistant/ring_analysis/camera_post_handlers.py` to run
  after analysis (e.g. `bedroom_emergency_alarm`).
- `pod_policy.conditions` — predicates against the analyzer's
  output. All must pass for a pod to mint. Operators: `equals`,
  `min`, `max`.
- `pod_policy.source_kind` — tag stamped onto the minted pod.
  Pods with this `source_kind` can be opted into dayflow ingest
  via `app/assistant/rooms/dayflow_orchestrator/access.json`'s
  `ingestion_pod_kinds` allowlist.
- `pod_policy.body_field` / `one_liner_field` — which
  analyzer-output fields become the pod body / one_liner.

## Pipeline flow per snapshot

```
ring_snapshot_captured event
  → camera_dispatcher (routes by camera_id from registry)
    → JPEG moved to <storage_folder>/YYYY-MM-DD/
    → analyzer agent runs (vision)
    → .txt sidecar + .meta.json sidecar written
    → pod_policy evaluated → mint image pod (or skip)
    → post_handlers run (emergency alarms, etc.)
       ↓
       pod_store
         ↓ (if source_kind in dayflow allowlist)
         dayflow ingest → item lifecycle
```

## To wire a new camera end-to-end

1. **Add the camera registry entry** in `configs/cameras.json`.
2. **Create the analyzer agent** if you don't have one already (see
   `extending-emi-agents`). Sample vision-analyzer agents:
   `app/assistant/agents/door_bell_analyzer/`,
   `app/assistant/agents/sleep_analyzer/`.
3. **For Ring cameras**: also add to
   `configs/smart_home_tools.json` under `ring.devices`.
4. **For local RTSP cameras**: add to `configs/smart_home_tools.json`
   under `local_cameras.devices`. Optionally schedule periodic
   capture by adding a routine entry that calls the
   `local_camera_snapshot` tool (see `extending-emi-routines`).
5. **Optional**: add a `post_handler` function to
   `camera_post_handlers.py` if the camera needs special handling
   beyond pod-minting (e.g. alarm on emergency frames).
6. **Optional**: add the new `pod_policy.source_kind` to
   `ingestion_pod_kinds` in dayflow access.json so the dayflow
   orchestrator picks up frames as artifacts.
7. **Restart Flask.** Camera registry loads at startup.

## Canonical examples

- Doorbell with significance gating:
  see `Front Door` entry in `configs/cameras.json`.
- Bedroom with cadence + emergency post-handler:
  see `Bedroom` entry; matching `sleep_camera_tick` routine in
  `configs/routines.json` for the periodic capture trigger.

## Notes

- The dispatcher is itself a routine (`camera_dispatch` in
  routines.json). If you remove/disable that routine, no camera
  works. Keep it enabled.
- Cameras share `data/cameras/<name>/` — the per-camera folder
  layout makes it trivial to wipe one camera's history without
  touching others.
- The previous-frame lookup (used by sleep-style two-frame compare
  analyzers) walks the camera's own folder + the prior date folder
  for midnight-rollover safety. New analyzer agents that want this
  pattern get it for free via the dispatcher's
  `agent_input.previous_image`.
