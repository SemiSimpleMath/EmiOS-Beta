"""master_room::response_formatter regression scenarios.

Worked example of scripts/prompt_iter.iterate — used to verify the
post-final_answer fix (commit 7bfe5d47) that this formatter relays
rather than re-paraphrases the rich content emi_team::final_answer
now provides.

Uses iterate (DI mode) because response_formatter's user_context_items
are all plain blackboard keys — no recent_history. The agent runs on
Gemini Flash; DI mode handles that.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.prompt_iter import Scenario, iterate


KARPATHY_TASK = "Find a conversation pod between Jukka Virtanen and Justin that mentions Andrew Karpathy."
KARPATHY_INFO = (
    "Search the pod store / conversation pod archive for entries involving both "
    "Jukka Virtanen and Justin where 'Andrew Karpathy' (and likely variants like "
    "'Karpathy') is mentioned. Return the pod id(s), timestamp(s), and a short "
    "snippet/summary so the user can confirm the right one."
)

KARPATHY_RAW_RESULT = """\
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

These two pods appear to be the Jukka-Justin conversations you're looking for that mention (Andrew/Andrej) Karpathy via his gist and pattern.
"""

RICH = Scenario(
    label="karpathy-rich",
    agent_input={
        "task": KARPATHY_TASK,
        "information": KARPATHY_INFO,
        "tool_result_for_formatter": KARPATHY_RAW_RESULT,
    },
    required_fragments=[
        "datapod:chat_cluster:c6b674d26788ff10a23c8230",
        "datapod:chat_cluster:e50d9caf22be3e6e4b9eeb1f",
        "2026-04-19T01:14:06",
        "2026-04-19T01:38:20",
        "Karpathy gist 404",
        "Jukka's knowledge graph",
    ],
    forbidden_fragments=["see below", "snippets below", "Pod IDs Found:"],
)

BRIEF_TASK = "Set the living room thermostat to 72F."
BRIEF = Scenario(
    label="thermostat-brief",
    agent_input={
        "task": BRIEF_TASK,
        "information": "User asked to set the thermostat. Confirm the action.",
        "tool_result_for_formatter": "Set the living room thermostat to 72F. Confirmed by nest_home_control.",
    },
    required_fragments=["72", "thermostat"],
    max_chars=250,
)

if __name__ == "__main__":
    iterate(
        "master_room::response_formatter",
        [RICH, BRIEF],
        runs=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
    )
