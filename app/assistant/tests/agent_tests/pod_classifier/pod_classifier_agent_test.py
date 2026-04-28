"""Iterative agent test for pod_classifier.

Feeds canned (burst, lookback) pairs into the agent and prints the
output plus the verbatim-subset validator verdict. Use this to iterate
on the prompt without waiting for real chat flushes.

Run:
    .venv/Scripts/python.exe app/assistant/tests/agent_tests/pod_classifier/pod_classifier_agent_test.py
"""
import app.assistant.tests.test_setup  # noqa: F401

from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


@dataclass
class Case:
    name: str
    burst: str
    chat_history: str = ""
    expect_tags_nonempty: bool = True


def load_tag_vocabulary() -> dict:
    with Path("configs/pod_tags.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("tags", {}) if isinstance(data, dict) else {}


# ---------- Canned cases derived from real session history ------------------


CASE_COFFEE_CREAMER = Case(
    name="coffee creamer + Bonnie portions (reproduces the 'unknown:' injection)",
    burst=(
        "- [04:56 PM] Jukka: I am thinking of using less creamer in coffee\n"
        "- [04:56 PM] Emi: That's a good move. It'll definitely let you taste the actual coffee more, and it fits right in with that 'healthy choices' vibe you've got going today.\n"
        "- [04:56 PM] Jukka: i bet over a whole day it adds up if i drink two to three cups.\n"
        "- [04:56 PM] Emi: Oh, for sure. Those calories and sugars definitely sneak up on you when you're a few cups in. It's a low-effort win for the 'healthy choices' streak you're on today.\n"
        "- [04:56 PM] Jukka: i use pretty high calorie creamer.\n"
        "- [04:56 PM] Emi: Yeah, those 'indulgent' ones are basically liquid ice cream. If you're doing three cups a day, that's a lot of extra sugar and calories you're cutting out just by scaling back.\n"
        "- [04:57 PM] Jukka: Bonnie is getting fat\n"
        "- [04:57 PM] Emi: Uh oh, Bonnie too? I guess she didn't get the memo about the new healthy-choices-only rule. Is she just scoring extra treats while you're distracted by the game?\n"
        "- [04:57 PM] Jukka: i am scaling back her food portions\n"
        "- [04:57 PM] Emi: She's going to give you the ultimate puppy-dog eyes when she sees the smaller bowl, but it's definitely the right call for her health."
    ),
    chat_history=(
        "- [01:00 PM] Jukka: I have never played read dead redemption II is it still worth it?\n"
        "- [01:00 PM] Emi: Oh, 100%. It's basically a playable masterpiece.\n"
        "- [02:30 PM] Jukka: just bought red dead redemption ii\n"
        "- [02:30 PM] Emi: Nice! You're in for a treat.\n"
        "- [04:10 PM] Jukka: i have decided that i can eat anything as long as its healthy\n"
        "- [04:10 PM] Emi: That's a solid rule to live by."
    ),
    expect_tags_nonempty=True,
)


CASE_RDR2_NO_SPOILERS = Case(
    name="RDR2 reference requires lookback to resolve",
    burst=(
        "Jukka: Hey this game is really good\n"
        "Emi: Told you! It's one of those games that just feels alive. Still in the snow or did you make it to Horseshoe Overlook yet?\n"
        "Jukka: no spoilers\n"
        "Emi: My bad! Lips are sealed. I'll let you discover everything at your own pace."
    ),
    chat_history=(
        "Jukka: just bought red dead redemption ii\n"
        "Emi: Nice! You're in for a treat. Just power through that snowy intro.\n"
        "Jukka: 9% downloaded so this will take an hour onto itself\n"
        "Emi: The longest hour ever.\n"
        "Jukka: it just finished dl\n"
        "Emi: Sweet! Time to saddle up.\n"
        "Jukka: bathroom break first\n"
        "Emi: Smart move.\n"
        "Jukka: ok i think i am ready to get on a horse\n"
        "Emi: Hell yeah. Saddle up!"
    ),
    expect_tags_nonempty=True,
)


CASE_PURE_CHITCHAT = Case(
    name="pure chitchat — should return empty tags",
    burst=(
        "Justin: hey\n"
        "Jukka: lol\n"
        "Justin: wasn't talking to you lol\n"
        "Jukka: haha fair"
    ),
    chat_history="",
    expect_tags_nonempty=False,
)


CASE_MIXED_TOPICS = Case(
    name="mixed burst: food + weather — should drop weather if tags come out food-only",
    burst=(
        "Jukka: having a yogurt and banana for breakfast\n"
        "Emi: nice light start\n"
        "Jukka: what's the weather in NYC\n"
        "Emi: 56F and clear\n"
        "Jukka: thanks, might head over later this week"
    ),
    chat_history="",
    expect_tags_nonempty=True,
)


CASE_FILLER_HEAVY = Case(
    name="food burst with lots of filler — filler should drop, food stays verbatim",
    burst=(
        "Jukka: lol\n"
        "Jukka: Time to have a yogurt\n"
        "Emi: Good call. A little fuel for the trail. Enjoy it!\n"
        "Jukka: ok\n"
        "Jukka: i have decided that i can eat anything as long as its healthy\n"
        "Emi: That's a solid rule to live by. Keeps things simple without feeling like you're restricted to a tiny list of 'allowed' foods. Enjoy that yogurt!\n"
        "Jukka: hello?"
    ),
    chat_history="",
    expect_tags_nonempty=True,
)


ALL_CASES: List[Case] = [
    CASE_COFFEE_CREAMER,
    CASE_RDR2_NO_SPOILERS,
    CASE_PURE_CHITCHAT,
    CASE_MIXED_TOPICS,
    CASE_FILLER_HEAVY,
]


# ---------- Runner ----------------------------------------------------------


def run_case(case: Case, vocab: dict) -> None:
    print("=" * 78)
    print(f"CASE: {case.name}")
    print("-" * 78)
    print("BURST:")
    print(case.burst)
    print()
    if case.chat_history:
        print(f"CHAT HISTORY ({len(case.chat_history.splitlines())} lines):")
        print(case.chat_history)
        print()

    agent = DI.agent_factory.create_agent("pod_classifier")
    result = agent.action_handler(
        Message(
            agent_input={
                "burst_content": case.burst,
                "tag_vocabulary": vocab,
                "chat_history": case.chat_history,
            }
        )
    )
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}

    tags = data.get("tags") or []
    one_liner = str(data.get("one_liner") or "")
    compact_body = str(data.get("compact_body") or "")
    reasoning = str(data.get("reasoning") or "")

    print("OUTPUT:")
    print(f"  tags       = {tags}")
    print(f"  one_liner  = {one_liner!r}")
    print(f"  reasoning  = {reasoning}")
    print("  compact_body:")
    for line in compact_body.splitlines():
        print(f"    | {line}")
    if not compact_body.strip():
        print("    (empty)")

    print()

    if case.expect_tags_nonempty and not tags:
        print("  EXPECTATION MISMATCH: expected non-empty tags, got []")
    if not case.expect_tags_nonempty and tags:
        print(f"  EXPECTATION MISMATCH: expected empty tags, got {tags}")

    print()


def main():
    vocab = load_tag_vocabulary()
    print(f"Tag vocabulary: {list(vocab.keys())}\n")
    for case in ALL_CASES:
        run_case(case, vocab)


if __name__ == "__main__":
    main()
