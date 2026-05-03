## How to disambiguate State / Event / Property nodes — instance vs category

State, Event, and Property nodes come in two shapes that look identical
at the label level but behave very differently when you decide whether
two of them refer to the same thing.

### Instance-shaped: anchored to a specific occurrence

Has at least one anchoring fact beyond the label — a date, a place, a
specific named role, a unique participant. Two instance-shaped nodes
match when their **anchor matches**, even if the participant set grows.
Phil being added as best man to "Wedding 2003 (Katy & Jukka, Maui)" is
genuine growth into the same instance — the anchor (couple + date +
place) didn't change, only the participant list.

Examples: a specific Wedding, "Espoo–Karjalohja Bike Ride", "School
drop-off (Annika at Foothill, 8am)", "Trip to Palm Desert 2024-03",
"Performance at South Lake Middle School 2025".

### Category-shaped: a generic label with no instance anchor

Just a label that names a relationship or activity category. Sibling
Relationship, Parenthood, Marriage, Friendship, Ownership, Age
Difference, Phone Contact Method, Residence (without dates/place),
Belief, Preference, Habit, Driving, Arrival, Visit, Meeting, Question,
Conversation, Trip (no modifier), Walk, Email Request, Email Contact.

These are NOT instances — they are kinds. Two category-shaped nodes are
the same instance ONLY if the participant sets are equal (or one is a
strict subset of the other) AND, for Events, the temporal anchors
match (see below).

A label match plus one shared participant is NOT enough. "Sibling
Relationship (Jukka, Seija)" and "Sibling Relationship (Dave, Matt)"
share neither pair — they are two distinct sibling pairs that happen
to share a category. Merging them on label alone produces a single
node where Seija appears as Dave's sister.

### Events: temporal identity is mandatory

A State can persist across time — "Marriage (Jukka, Katy)" doesn't get
a new node every day. An Event is an OCCURRENCE — it happens at a
specific time and place. Even when participants are identical, two
Events on different dates are TWO Events, not one.

"Driving" today and "Driving" yesterday with the same driver are TWO
events. "Arrival" at LAX in 1990 and "Arrival" at SFO in 2024 are TWO
events even if the family is the same. "Question" asked Tuesday and
"Question" asked Friday are TWO events even if asker and topic match.

For Event candidates, require BOTH participant equality (per above) AND
temporal proximity. The acceptable temporal gap depends on the event's
typical duration — match the tolerance to the scale of the event:

- **Long-running Events** (Wedding, Trip, Relocation, Conference,
  School Year): hours-to-days difference may still be the same event.
- **Daily-scale Events** (Dinner, Meeting, Visit, Drive, Drop-off):
  same calendar day required; different days = different instances.
- **Short Events** (Dog walk, Phone call, Chat session, Question,
  Conversation, Email, Notification): **same hour or closer**. A walk
  this morning and a walk this evening are TWO walks, not one.

Generic-label Events (Driving, Arrival, Visit, Meeting, Conversation,
Walk, Trip without modifier, Question) are the easiest to over-merge
because the label invites any new instance to attach. Default to
creating a new node for these unless both temporal and participant
identity match at the appropriate scale.

### Properties: subject identity is mandatory

A Property node represents a characteristic OF a subject (Date of
Birth, Phone Number, Place of Birth, Email Address, Age, Height). Each
subject has their own Property node — "Date of Birth" for Jouko is a
DIFFERENT node from "Date of Birth" for Jaime. The label is shared by
convention; the identity is per-subject.

Two Property nodes are the same node ONLY if they share at least one
subject (the entity connected to the property via has_property or a
similar predicate). Label match alone is never enough. Without a
subject match, create a new Property node.

### Known generic labels that have produced over-merges in this graph

Treat these as category-shaped by default unless an instance anchor is
explicit in the data:

- Sibling Relationship, Siblinghood
- Parenthood, Parent-Child Relationship
- Age Difference
- Phone Contact Method
- School Enrollment (without specific student + school anchor)
- Marriage (without date or place anchor)
- Friendship, Acquaintance
- Ownership, Possession
- Belief, Preference, Habit (when the label has no topic suffix)

### Decision rule

When a candidate has the same label as a new node:

1. Is the label category-shaped (one of the above, or similarly generic)?
   → require **participant equality** (or strict subset for the growth
   case). Otherwise create a new node.
2. Is the label instance-shaped (specific date / place / role)?
   → match on the anchor; grow the participant set if the new node adds
   a participant to the same instance.

### Default

When in doubt, do not merge. A duplicate is fixable by a later sweep.
A bad merge cross-links unrelated subgraphs and is much harder to
undo.
