"""Live e2e: slack/__test__ asks for the karpathy pod.

Runnable script (NOT a pytest test — costs real LLM tokens). Seeds a fake
karpathy chat_cluster pod tagged with the slack test room's scope_id, then
asks the test slack room to find it. Reports:

  - per_manager rules in the active scope
  - final response text
  - LLM cost breakdown
  - any tool calls observed in the run
  - elapsed time

Run::

    .venv\\Scripts\\python.exe -m app.assistant.tests.room_test_harness.live_karpathy_pod

Expected to validate:
  1. Slack scope per_manager.allow narrows emi_team to web/pod/operational only
  2. pod_search picks up the seeded karpathy pod (scope-filtered, not the real DB)
  3. Final response references the pod (or at least the matching content)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.assistant.tests.room_test_harness.harness import simulate_room_inbound


SLACK_TEST_ROOM_ID = "slack/__test__"


def seed_karpathy_pod() -> str:
    """Insert a fake chat_cluster pod scoped to the test slack room.

    Returns the pod_id so we can verify it shows up in the manager trace.
    """
    from app.assistant.pod_store.contracts import Pod, PodSourceRef
    from app.assistant.pod_store.pod_store import PodStore

    pod = Pod(
        pod_id="datapod:chat_cluster:e2e_karpathy_demo",
        kind="chat_cluster",
        tags=["karpathy", "slack", "ai_research"],
        one_liner=(
            "Justin and Jukka discussed Karpathy's latest video on building "
            "transformer models from scratch — Justin asked about the optimizer "
            "choices and Jukka shared a link to the GitHub repo."
        ),
        body=(
            "Justin: Have you seen the new Karpathy video? The one where he builds "
            "a GPT from scratch in pytorch?\n"
            "Jukka: Yeah, I watched it last night. The way he explains attention is "
            "really clean. The Adam vs AdamW comparison was useful too.\n"
            "Justin: I was confused about why he chose RMSNorm over LayerNorm — did "
            "he explain that?\n"
            "Jukka: He didn't go deep on it; said it's just simpler computationally. "
            "Here's his repo: https://github.com/karpathy/nanoGPT\n"
        ),
        source_refs=[PodSourceRef(kind="unified_log", id="e2e_test_synthetic")],
        for_agents=[],
        scope_id=SLACK_TEST_ROOM_ID,  # critical — pod_search filters by this
        created_by="e2e_test_seed",
        created_at=datetime.now(timezone.utc),
        metadata={"critic_content_type": "discussion"},
    )
    PodStore().put(pod)
    return pod.pod_id


def main():
    print("=" * 70)
    print("LIVE E2E: slack/__test__ → 'find karpathy pod' (real LLMs, sandboxed DB)")
    print("=" * 70)

    seeded_id = None

    def _setup():
        nonlocal seeded_id
        seeded_id = seed_karpathy_pod()
        print(f"\n[setup] Seeded pod: {seeded_id}")

    result = simulate_room_inbound(
        surface="slack",
        room_id=SLACK_TEST_ROOM_ID,
        content="Emi can you find the pod where justin and i talked about karpathy's wiki idea?",
        speaker_name="Jukka",
        speaker_external_id="U_jukka_test",
        setup_fn=_setup,
    )

    print("\n" + "-" * 70)
    print("SCOPE (per_manager rules in effect)")
    print("-" * 70)
    pm = result.scope_dict.get("tools", {}).get("per_manager", {})
    print(json.dumps(pm, indent=2))
    print(f"authority_level: {result.scope_dict.get('approval', {}).get('authority_level')}")

    print("\n" + "-" * 70)
    print("FINAL RESPONSE")
    print("-" * 70)
    print(result.final_response or "(no response — silence / no_op)")

    print("\n" + "-" * 70)
    print(f"ELAPSED: {result.elapsed_ms} ms  |  LLM COST: ${result.total_cost_usd():.4f}  |  CALLS: {len(result.llm_calls)}")
    print("-" * 70)
    if result.llm_calls:
        print(f"{'agent':<45} {'in':>7} {'cached':>7} {'out':>7} {'cost':>8}")
        for c in result.llm_calls:
            print(f"{c.agent_name[:44]:<45} {c.input_tokens:>7} {c.cached_tokens:>7} {c.output_tokens:>7} ${c.cost_usd:>6.4f}")

    print("\n" + "-" * 70)
    print("MANAGER RESULT")
    print("-" * 70)
    mr = result.manager_result
    if hasattr(mr, "content"):
        print(f"content: {mr.content[:500] if mr.content else '(empty)'}")
    if hasattr(mr, "data") and mr.data:
        print(f"data keys: {list(mr.data.keys()) if isinstance(mr.data, dict) else type(mr.data).__name__}")

    print("\n" + "-" * 70)
    print(f"SEEDED POD ID: {seeded_id}")
    print(f"Found in response: {seeded_id in (result.final_response or '')}")
    print("-" * 70)


if __name__ == "__main__":
    main()
