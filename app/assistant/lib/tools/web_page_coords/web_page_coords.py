from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.web_mcp_utils import mcp_call
from app.assistant.utils.json_parsing import parse_jsonish
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)

_INJECT_MARKS_JS = (Path(__file__).parent / "inject_marks.js").read_text(encoding="utf-8")


def _pick_first_image_path(attachments: list[dict[str, Any]] | None) -> Optional[str]:
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "image":
            continue
        p = a.get("path")
        if isinstance(p, str) and p.strip():
            try:
                return str(Path(p).resolve())
            except Exception as e:
                logger.debug("[web_page_coords] could not resolve image path %r: %s", p, e)
                return p.strip()
    return None


class WebPageCoords(BaseTool):
    """
    Primary page-understanding tool for Playwright MCP browsing.

    Always-on marks mode:
    - Programmatically find likely clickable elements + draw numbered overlays in the live page
    - Take a screenshot with marks
    - Use a vision mark-picker to choose mark id(s)
    - Return deterministic coords from the chosen mark(s) using the in-memory marks list
      (no second browser round-trip needed)
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_TOOL = "browser_take_screenshot"
    # playwright-mcp 2026 upgrade removed `browser_run_code` and replaced it
    # with `browser_evaluate`. The new tool runs JS IN the page context
    # (`() => { ... }`) instead of in the Playwright-side context
    # (`async (page) => { ... }`). inject_marks.js was refactored to a pure
    # DOM-side body that uses `q` and `MAX` from outer scope; the wrapper
    # below provides those constants.
    MCP_EVALUATE = "browser_evaluate"
    MAX_MARKS = 50

    def __init__(self):
        super().__init__("web_page_coords")

    def _inject_marks(self, *, server_entry: dict, question: str, max_marks: int = MAX_MARKS) -> list[dict[str, Any]]:
        """
        Inject numbered overlay marks into the live page and return the full marks list.

        inject_marks.js is the function body — Python wraps it in `() => { ... }`
        with `q` and `MAX` constants prepended so the body can reference them.
        Runs via the `browser_evaluate` MCP tool which executes in the page
        context (no Playwright-side `page` object available; the hover-probe
        phase that needed `page.mouse.move` was dropped).

        Returns list of {id, x, y, label, rect} dicts — the Python caller uses this
        directly to resolve coords after vision picks mark ids, eliminating the
        second browser round-trip that _fetch_marks_by_ids used to require.
        """
        wrapper = (
            "() => {\n"
            f"  const q = {_json.dumps(question or '')};\n"
            f"  const MAX = {int(max_marks)};\n"
            f"{_INJECT_MARKS_JS}\n"
            "}"
        )

        text, is_error, _atts = mcp_call(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": wrapper},
        )
        if is_error:
            raise RuntimeError(text or "browser_evaluate returned isError")
        parsed = parse_jsonish(text)
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        preview = (text or "")[:1200]
        raise RuntimeError(f"browser_evaluate marks parse failed — expected list. preview={preview!r}")

    def _remove_marks_overlay(self, *, server_entry: dict) -> None:
        js = """
() => {
  try {
    if (window.__emi_marks_overlay) {
      window.__emi_marks_overlay.remove();
      window.__emi_marks_overlay = null;
    }
    window.__emi_marks_map = null;
  } catch (e) {}
  return true;
}
""".strip()
        try:
            mcp_call(server_entry=server_entry, tool_name=self.MCP_EVALUATE, arguments={"function": js})
        except Exception as e:
            logger.debug("[web_page_coords] overlay removal failed (non-fatal): %s", e)

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        question = (args.get("question") or "").strip()
        strict = bool(args.get("strict", True))

        def _fatal(msg: str, *, data: Optional[dict[str, Any]] = None) -> None:
            banner = "\n".join(
                [
                    "=" * 90,
                    "FATAL: web_page_coords strict failure",
                    "=" * 90,
                    msg,
                    "=" * 90,
                ]
            )
            logger.error(banner)
            print(banner)
            if data:
                try:
                    print(_json.dumps(data, indent=2, default=str))
                except Exception:
                    print(str(data))
            raise RuntimeError(msg)

        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception as e:
            logger.debug("[web_page_coords] could not resolve server entry: %s", e)
            server_entry = None

        if not isinstance(server_entry, dict):
            _fatal(
                f"MCP server entry missing for {self.SERVER_ID}.",
                data={"server_id": self.SERVER_ID},
            )

        marks: list[dict[str, Any]] = []
        marked = False
        try:
            marks = self._inject_marks(server_entry=server_entry, question=question, max_marks=self.MAX_MARKS)
            marks_count = len(marks)
            marked = marks_count > 0
        except Exception as e:
            logger.warning(
                "[web_page_coords] MARK INJECTION FAILED — question=%r error=%s",
                question, e,
            )
            if strict:
                _fatal(
                    f"Mark overlay injection failed: {e}",
                    data={"question": question},
                )
            marks_count = 0
            marked = False

        if not marked and strict:
            logger.warning(
                "[web_page_coords] PAGE NOT READY — zero marks found. "
                "Page may still be loading or the browser navigated away. question=%r",
                question,
            )
            return ToolResult(
                result_type="web_page_coords",
                content="Page was not ready; try again now.",
                data={
                    "question": question or None,
                    "strict": bool(strict),
                    "marked": False,
                    "marks_count": 0,
                    "requested_target_found": False,
                    "image_path": None,
                    "attachments": [],
                    "vision": {},
                    "targets": [],
                    "not_ready": True,
                },
            )

        if marked and marks_count >= self.MAX_MARKS:
            logger.warning(
                "[web_page_coords] CAP HIT — %d marks injected (at cap=%d). "
                "The target element might be mark #%d+. question=%r",
                marks_count, self.MAX_MARKS, self.MAX_MARKS + 1, question,
            )

        try:
            attachments: list[dict[str, Any]] = []
            try:
                _text, is_error, attachments = mcp_call(
                    server_entry=server_entry,
                    tool_name=self.MCP_TOOL,
                    arguments={"type": "png", "fullPage": False},
                )
                if is_error:
                    raise RuntimeError(_text or "MCP screenshot returned isError")
            except Exception as e:
                _fatal(
                    f"Screenshot failed: {e}",
                    data={"tool": self.MCP_TOOL},
                )

            image_path = _pick_first_image_path(attachments)
            if not image_path:
                logger.error(
                    "[web_page_coords] SCREENSHOT SAVED NO IMAGE — "
                    "browser_take_screenshot returned no image attachment. "
                    "marks_count=%d question=%r attachments=%r",
                    marks_count, question, attachments,
                )
                _fatal(
                    "Screenshot produced no persisted image attachment.",
                    data={"attachments": attachments},
                )

            vision_payload: dict[str, Any] = {}
            out_targets: list[dict[str, Any]] = []
            requested_target_found: bool = True

            if marked and marks_count > 0:
                try:
                    picker = DI.agent_factory.create_agent("shared::vision_mark_picker")
                    if picker is None:
                        raise RuntimeError("DI.agent_factory could not create shared::vision_mark_picker")

                    task = question or "Pick the best mark(s) to dismiss any modal/overlay (close X / accept cookies / continue)."
                    msg = Message(
                        agent_input={
                            "date_time": get_local_time_str(),
                            "task": task,
                            "image": image_path,
                        }
                    )
                    res = picker.action_handler(msg)
                    if hasattr(res, "data") and isinstance(res.data, dict):
                        vision_payload = res.data
                    elif isinstance(res, dict):
                        vision_payload = res
                    else:
                        vision_payload = {"raw": str(res)}

                    mark_ids = vision_payload.get("mark_ids") if isinstance(vision_payload, dict) else None
                    if not isinstance(mark_ids, list) or not mark_ids:
                        requested_target_found = False
                        mark_ids = []
                        logger.warning(
                            "[web_page_coords] VISION FOUND NO MATCH — "
                            "vision_mark_picker returned no mark_ids for question=%r. "
                            "The requested element is not visible or the question did not match any badge. "
                            "marks_count=%d image=%s",
                            question, marks_count, image_path,
                        )

                    if requested_target_found:
                        want_ids: list[int] = []
                        for x in mark_ids:
                            if isinstance(x, int):
                                want_ids.append(int(x))
                            elif isinstance(x, str) and x.strip().isdigit():
                                want_ids.append(int(x.strip()))

                        if not want_ids:
                            logger.error(
                                "[web_page_coords] UNPARSEABLE MARK IDS — "
                                "vision returned mark_ids=%r which could not be parsed as ints. "
                                "question=%r marks_count=%d image=%s",
                                mark_ids, question, marks_count, image_path,
                            )
                            _fatal(
                                "Vision mark picker mark_ids were not parseable ints (strict).",
                                data={"mark_ids": mark_ids, "image_path": image_path},
                            )

                        invalid_ids = [mid for mid in want_ids if mid < 1 or mid > int(marks_count)]
                        if invalid_ids:
                            logger.error(
                                "[web_page_coords] OUT-OF-RANGE MARK IDS — "
                                "vision returned ids=%r but only %d marks exist. "
                                "Invalid ids=%r. "
                                "This usually means the vision model misread a badge number "
                                "(e.g. blurry screenshot after aggressive browser_resize). "
                                "question=%r image=%s",
                                want_ids, marks_count, invalid_ids, question, image_path,
                            )
                            _fatal(
                                "Vision mark picker returned out-of-range mark ids (strict).",
                                data={
                                    "mark_ids": want_ids,
                                    "invalid_ids": invalid_ids,
                                    "marks_count": int(marks_count),
                                    "image_path": image_path,
                                },
                            )

                        # Resolve coords from the in-memory marks list — no browser round-trip needed.
                        marks_by_id = {m["id"]: m for m in marks if isinstance(m, dict) and "id" in m}
                        chosen_marks = [marks_by_id[mid] for mid in want_ids if mid in marks_by_id]
                        if not chosen_marks:
                            logger.error(
                                "[web_page_coords] MARKS MAP MISS — "
                                "in-memory marks had no entry for ids=%r. "
                                "This should not happen unless injection returned an unexpected format. "
                                "question=%r image=%s",
                                want_ids, question, image_path,
                            )
                            _fatal(
                                "Could not look up chosen mark ids in in-memory marks list (strict).",
                                data={"want_ids": want_ids, "image_path": image_path},
                            )

                        for m in chosen_marks[:3]:
                            out_targets.append(
                                {
                                    "purpose": "marked_click",
                                    "x": float(m.get("x")),
                                    "y": float(m.get("y")),
                                    "rationale": f"Selected mark id={m.get('id')} label={m.get('label')!r}",
                                    "mark_id": m.get("id"),
                                    "label": m.get("label"),
                                }
                            )
                except Exception as e:
                    logger.error(
                        "[web_page_coords] VISION PICKER CRASHED — "
                        "shared::vision_mark_picker raised an exception. "
                        "question=%r marks_count=%d image=%s error=%s",
                        question, marks_count, image_path, e,
                    )
                    _fatal(
                        f"Vision mark picker failed: {e}",
                        data={
                            "agent": "shared::vision_mark_picker",
                            "image_path": image_path,
                            "marks_count": int(marks_count),
                        },
                    )

            if not out_targets and requested_target_found:
                logger.error(
                    "[web_page_coords] PIPELINE PRODUCED NO TARGETS — "
                    "marks were injected and vision ran but out_targets is empty. "
                    "question=%r marks_count=%d image=%s vision=%r",
                    question, marks_count, image_path, vision_payload,
                )
                _fatal(
                    "No targets produced from marks pipeline.",
                    data={"image_path": image_path, "marked": marked, "vision": vision_payload},
                )

            n_targets = len(out_targets) if isinstance(out_targets, list) else 0
            if n_targets <= 0 and requested_target_found:
                logger.error(
                    "[web_page_coords] ZERO TARGETS (strict) — "
                    "question=%r marks_count=%d image=%s",
                    question, marks_count, image_path,
                )
                _fatal(
                    "No targets produced (strict).",
                    data={"image_path": image_path, "marked": marked},
                )

            return ToolResult(
                result_type="web_page_coords",
                content=(
                    "web_page_coords: screenshot captured and analyzed."
                    f" marked={bool(marked)} marks={int(marks_count)} targets={n_targets}"
                    f" requested_target_found={bool(requested_target_found)}"
                ),
                data={
                    "question": question or None,
                    "strict": bool(strict),
                    "marked": bool(marked),
                    "marks_count": int(marks_count),
                    "requested_target_found": bool(requested_target_found),
                    "image_path": image_path,
                    "attachments": attachments,
                    "vision": vision_payload,
                    "marks": None,
                    "targets": out_targets,
                },
            )
        finally:
            if marked:
                self._remove_marks_overlay(server_entry=server_entry)


def get_tool_class():
    return WebPageCoords
