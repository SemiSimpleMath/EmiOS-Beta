"""New belief intake: one day at a time — create that day's atomic beliefs, check them for duplicates against the
beliefs held, apply the verdicts. See docs/design/belief_intake_redesign_2026-09-26.md.

This package currently builds a SEPARATE replay store (scratch/belief_replay.db) from the stored
daily timelines; it never writes to the live belief tables.
"""
