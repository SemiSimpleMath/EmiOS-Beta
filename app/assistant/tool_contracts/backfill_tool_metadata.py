from __future__ import annotations

import json
from pathlib import Path

from app.assistant.utils.path_utils import get_repo_root

ROOT = get_repo_root()
TOOLS_DIR = ROOT / "app" / "assistant" / "lib" / "tools"


FRONT_DOOR = {
    "ask_user",
    "find_tool",
    "install_tool",
    "get_calendar_events",
    "get_email",
    "get_todo_tasks",
    "search_web",
    "notify_user",
}


def _category(name: str) -> str:
    if name.startswith("kg_"):
        return "knowledge_graph"
    if name.startswith("taxonomy_"):
        return "knowledge_graph"
    if name.startswith("web_") or name in {"search_web", "scrape_url", "playwright_browse", "playwright_page_overview", "playwright_manager"}:
        return "web"
    if name.startswith("vision_") or name.startswith("capture_") or name in {"set_screen_capture_enabled", "web_visual_scout"}:
        return "vision"
    if "calendar" in name:
        return "calendar"
    if "email" in name:
        return "email"
    if "todo" in name:
        return "todo"
    if "scheduler" in name or "event_tree" in name:
        return "scheduler"
    if "text_file" in name or "write_task_outputs" in name:
        return "files"
    if name.endswith("_manager") or name in {"run_compiled_task", "run_job_spec", "run_routine", "one_shot_tool_runner"}:
        return "orchestration"
    if name in {"ask_user", "notify_user", "update_user_preference"}:
        return "user_interaction"
    if name in {"find_tool", "install_tool", "read_tool_result"}:
        return "tooling"
    return "general"


def _verbs(name: str) -> list[str]:
    verbs: list[str] = []
    for prefix, verb in [
        ("get_", "read"),
        ("find_", "search"),
        ("search_", "search"),
        ("create_", "create"),
        ("update_", "update"),
        ("delete_", "delete"),
        ("cancel_", "delete"),
        ("write_", "write"),
        ("append_", "write"),
        ("read_", "read"),
        ("run_", "run"),
        ("set_", "update"),
        ("install_", "install"),
        ("notify_", "notify"),
    ]:
        if name.startswith(prefix):
            verbs.append(verb)
    if not verbs:
        if name.endswith("_manager"):
            verbs = ["orchestrate"]
        else:
            verbs = ["operate"]
    return sorted(set(verbs))


def _entities(name: str, category: str) -> list[str]:
    entities = set()
    if "calendar" in name:
        entities.add("event")
    if "email" in name:
        entities.add("email")
    if "todo" in name:
        entities.add("task")
    if "scheduler" in name:
        entities.add("schedule")
    if name.startswith("kg_") or name.startswith("taxonomy_"):
        entities.update({"node", "edge", "graph"})
    if "text_file" in name:
        entities.add("file")
    if name in {"ask_user", "notify_user", "update_user_preference"}:
        entities.add("user")
    if category == "orchestration":
        entities.add("workflow")
    if not entities:
        entities.add(category if category != "general" else "tool")
    return sorted(entities)


def _risk_and_side_effects(name: str, category: str) -> tuple[str, str]:
    if name.startswith("delete_") or name.startswith("cancel_"):
        return "high", "destructive"
    if name.startswith("install_"):
        return "high", "writes_data"
    if name.startswith(("create_", "update_", "write_", "append_", "set_", "link_")):
        return "medium", "writes_data"
    if name.startswith(("send_", "notify_")):
        return "medium", "external_action"
    if category in {"orchestration"} and name.startswith("run_"):
        return "medium", "writes_data"
    return "low", "read_only"


def _requires_auth(name: str, category: str) -> list[str]:
    out = []
    if category in {"calendar", "email", "todo", "scheduler"}:
        out.append("google")
    if "slack" in name:
        out.append("slack")
    if "twilio" in name:
        out.append("twilio")
    return out


def _requires_network(category: str) -> bool:
    return category in {"calendar", "email", "todo", "scheduler", "web", "vision", "knowledge_graph"}


def _cost_latency(category: str, name: str) -> tuple[str, str]:
    if category in {"vision", "web"}:
        return "high", "slow"
    if category in {"knowledge_graph", "orchestration"}:
        return "medium", "moderate"
    if name.endswith("_manager"):
        return "medium", "moderate"
    return "low", "fast"


def _visibility_default(risk: str, side_effects: str, front_door: bool) -> str:
    if front_door:
        return "show"
    if risk == "high" or side_effects == "destructive":
        return "hide"
    return "conditional"


def build_metadata(tool_name: str) -> dict:
    category = _category(tool_name)
    verbs = _verbs(tool_name)
    entities = _entities(tool_name, category)
    risk_level, side_effects = _risk_and_side_effects(tool_name, category)
    requires_auth = _requires_auth(tool_name, category)
    requires_network = _requires_network(category)
    cost_level, latency_class = _cost_latency(category, tool_name)
    front_door = tool_name in FRONT_DOOR
    approval_required = risk_level in {"high", "critical"} or side_effects == "destructive" or tool_name == "install_tool"
    room_visibility_default = _visibility_default(risk_level, side_effects, front_door)

    return {
        "category": category,
        "verbs": verbs,
        "entities": entities,
        "risk_level": risk_level,
        "side_effects": side_effects,
        "requires_auth": requires_auth,
        "requires_network": requires_network,
        "cost_level": cost_level,
        "latency_class": latency_class,
        "front_door": front_door,
        "room_visibility_default": room_visibility_default,
        "approval_required": approval_required,
    }


def main() -> None:
    contracts = sorted(TOOLS_DIR.glob("*/tool_contract.json"))
    if not contracts:
        raise RuntimeError(f"No tool contracts found under {TOOLS_DIR}")

    changed = 0
    for p in contracts:
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            continue
        tool_name = str(raw.get("name") or p.parent.name).strip()
        if not tool_name:
            continue
        raw["metadata"] = build_metadata(tool_name)
        with p.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(raw, f, indent=2, ensure_ascii=True)
            f.write("\n")
        changed += 1

    print(f"Updated metadata for {changed} tool contracts.")


if __name__ == "__main__":
    main()

