# Orchestrator User Preferences

This file is a **template**. Edit it to capture your personal orchestrator
preferences — the dayflow / situation_auditor / chat_gate agents read it as
context when deciding what to surface or skip. Examples below are illustrative;
replace with your own.

## Reminders & Suggestions
Wellness reminders (hydration, stretches) are typically handled by other cron
events. If you want the orchestrator to also nag, say so here; otherwise it
will defer.

## General Directives
- Notify on context changes (location, schedule, important events).
- Proactively suggest ToDo tasks when appropriate.
- (Add your own behavioral preferences here — e.g. "ask me about X when Y".)

## Communication Escalation Policy
1. **Normal** — ticket (popup in UI). This is the primary way to reach you.
2. **Emergency, you are AFK** — send email to your primary address.
3. **Extreme emergency** — contact your designated emergency contact
   (configure name + channel in Settings → People).
