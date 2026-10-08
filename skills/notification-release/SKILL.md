---
name: notification-release
description: For the work architect. Who decides when a notification node reaches the owner. The architect suggests the release time; the state mover, which watches the wider context, decides when to release it. Change a node's time only when something actionable compels it.
metadata:
  author: jukka
  version: "1.0"
---

# When notifications reach the owner

A notification node is a message reaching the owner: a reminder, a heads-up, a question, a check-in. You have
full control over when it is released, through its time where it has one.

The time you write is the suggested time of release. Another agent, the state mover, watches the greater context
and decides when to actually release it: what the owner is doing, a quiet period they asked for, whether this is
a good moment. Trust it, and do not interfere with it.

Change the time of a node only when something actionable compels it: the event it is about moved, the owner asked
for another time, the goal changed. A wish not to be disturbed is already the state mover's to honour.
