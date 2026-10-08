---
name: notification-release
description: For the work architect. Who decides when a notification node reaches the owner. The architect writes when to release it (a suggested time, or the event that releases it); the state mover, which watches the wider context and receives the triggers, decides when to release it. Change a node's time only when something actionable compels it.
metadata:
  author: jukka
  version: "1.0"
---

# When notifications reach the owner

A notification node is a message reaching the owner: a reminder, a heads-up, a question, a check-in. You have
full control over when it is released, through its time where it has one.

A node can also be released by an event instead of a time: an email arriving, a reply, some other internal
trigger. Write the condition on the node (its wake_ref) and leave it there: the state mover receives these
triggers too and releases the node when its condition is met. The node must carry the instruction on when to
release it; that is all you need to give.

The time you write is the suggested time of release. Another agent, the state mover, watches the greater context
and decides when to actually release it: what the owner is doing, a quiet period they asked for, whether this is
a good moment. Trust it, and do not interfere with it.

Change the time of a node only when something actionable compels it: the event it is about moved, the owner asked
for another time, the goal changed. A wish not to be disturbed is already the state mover's to honour.
