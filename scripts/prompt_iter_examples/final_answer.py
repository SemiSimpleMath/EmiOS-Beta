"""emi_team::final_answer regression scenarios.

Worked example of scripts/prompt_iter.iterate_jinja — used to fix the
Karpathy data-loss bug (commit dbda9f1f). Re-run after editing
app/assistant/agents/emi_team/final_answer/prompts/system.j2 or user.j2.

Why iterate_jinja (not iterate): emi_team::final_answer's
user_context_items includes `recent_history`, which the framework
demands from a live scope with messages. We supply our own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.prompt_iter import Scenario, iterate_jinja


TASK = "Find a conversation pod between Jukka Virtanen and Justin that mentions Andrew Karpathy."
INFORMATION = (
    "Search the pod store / conversation pod archive for entries involving both "
    "Jukka Virtanen and Justin where 'Andrew Karpathy' (and likely variants like "
    "'Karpathy') is mentioned. Return the pod id(s), timestamp(s), and a short "
    "snippet/summary so the user can confirm the right one."
)

PLANNER_RETURN_CONTROL = """\
Here are the conversation pods between Jukka Virtanen and Justin that mention Karpathy, with timestamps and short snippets:

1) Pod ID: datapod:chat_cluster:c6b674d26788ff10a23c8230
   Kind/tags: chat_cluster [work]
   Scope: slack/C08AB0R54HM
   Timestamp: 2026-04-19T01:14:06
   One-liner: "Karpathy gist 404; KG mapping"
   Snippet:
   - Justin Shih: "<https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>"
   - Justin Shih: "How come slackbot isn't 'importing' it"
   - Jukka Virtanen: "This is kinda sick btw. This could be one way to do the knowledge graph."
   - The body then includes an explanation of Karpathy's three-layer pattern (immutable raw sources -> LLM-maintained wiki -> schema/workflow) and how it maps onto Jukka's KG-backed memory system.

2) Pod ID: datapod:chat_cluster:e50d9caf22be3e6e4b9eeb1f
   Kind/tags: chat_cluster [work]
   Scope: slack/C08AB0R54HM
   Timestamp: 2026-04-19T01:38:20
   One-liner: "Jukka's knowledge graph: A->S<-B"
   Snippet:
   - Jukka Virtanen describes his KG pattern using an example marriage sentence: entities A (Jukka), B (Katy), and a state S (marriage) linked as A -> S <- B with S carrying date/state info.
   - Summary line in the pod: "Justin shared Karpathy's gist; Jukka explained that Karpathy's three-layer pattern (raw sources -> LLM-maintained wiki -> index/log) maps to a KG-backed memory as: raw sources -> KG/evidence layer -> generated wiki views (wiki pages are synthesized, not identical to KG nodes)."
   - The rest of the conversation continues with Justin asking about whether the KG records them getting mad at each other, Claude code skills, and how Emi captures and uses Jukka's preferences.

These two pods appear to be the Jukka-Justin conversations you're looking for that mention (Andrew/Andrej) Karpathy via his gist and pattern. If you need me to narrow to exactly one (e.g., the first time Karpathy is brought up), the best candidate is pod `datapod:chat_cluster:c6b674d26788ff10a23c8230` with the direct gist link and initial KG mapping discussion.
"""

KARPATHY_HISTORY = f"""\
[12:32] Jukka asked: {TASK}

[12:33] tool_call: pod_search('karpathy', kind='chat_cluster')
tool_result: 2 pods returned (datapod:chat_cluster:c6b6...8230, datapod:chat_cluster:e50d...eb1f).

[12:34] tool_call: pod_fetch(['datapod:chat_cluster:c6b6...8230', 'datapod:chat_cluster:e50d...eb1f'])
tool_result: full bodies returned (Slack threads about Karpathy gist + KG mapping).

[12:35] emi_team::planner return_control. action_input:
{PLANNER_RETURN_CONTROL}
"""

RICH = Scenario(
    label="karpathy-rich",
    agent_input={
        "task": TASK,
        "information": INFORMATION,
        "recent_history": KARPATHY_HISTORY,
    },
    required_fragments=[
        "datapod:chat_cluster:c6b674d26788ff10a23c8230",
        "datapod:chat_cluster:e50d9caf22be3e6e4b9eeb1f",
        "2026-04-19T01:14:06",
        "2026-04-19T01:38:20",
        "Karpathy gist 404",
        "Jukka's knowledge graph",
    ],
)

BRIEF_TASK = "Set the living room thermostat to 72F."
BRIEF = Scenario(
    label="thermostat-brief",
    agent_input={
        "task": BRIEF_TASK,
        "information": "User asked to set the thermostat. Confirm the action.",
        "recent_history": (
            f"[09:15] Jukka asked: {BRIEF_TASK}\n"
            "[09:15] tool_call: nest_home_control(thermostat='living_room', target_f=72)\n"
            "tool_result: ok — current=68F, target set to 72F.\n"
            "[09:15] emi_team::planner return_control. action_input:\n"
            "Set the living room thermostat to 72F. Confirmed by nest_home_control.\n"
        ),
    },
    required_fragments=["72", "thermostat"],
    max_chars=300,
)

if __name__ == "__main__":
    iterate_jinja(
        "emi_team::final_answer",
        [RICH, BRIEF],
        runs=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
        model="gpt-5-mini",
    )
