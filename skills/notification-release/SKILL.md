---
name: notification-release
description: For the work architect. Who decides when a notification node reaches the owner. The architect writes when it is meant to go out (a time, or the event that releases it); the state mover, which watches the wider context and receives the triggers, decides when to actually release it. Change a node's time only when something actionable compels it.
metadata:
  author: jukka
  version: "1.1"
---

# When notifications reach the owner

A notification node is a message reaching the owner: a reminder, a heads-up, a question, a check-in.

You write when it is meant to go out: a time (wake_at), or the event that should release it (wake_ref), such as
an email arriving, a reply, or another trigger. The state mover receives those triggers too. It watches the
greater context (what the owner is doing, a quiet period they asked for, whether this is a good moment) and
decides when to actually release the node. Trust it and do not interfere with it: the node only has to carry
the instruction on when to release it.

Change the time of a node only when something actionable compels it: the event it is about moved, the owner
asked for another time, the goal changed. A wish not to be disturbed is the state mover's to honour, not a
reason to plan anything.
