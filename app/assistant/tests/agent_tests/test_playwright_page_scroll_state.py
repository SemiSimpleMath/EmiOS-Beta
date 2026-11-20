"""The page scroll probe parses browser_evaluate's reply (2026-11-20: it called a removed helper and always failed)."""
import pytest

from app.assistant.control_nodes.playwright_page_state_node import PlaywrightPageStateNode


def _node(raw: str) -> PlaywrightPageStateNode:
    node = PlaywrightPageStateNode.__new__(PlaywrightPageStateNode)
    node.name = "playwright_page_state_node"
    node._run_js = lambda *, server_entry, js: raw
    return node


def test_scroll_state_parsed_from_evaluate_reply():
    raw = (
        "### Result\n"
        '{"scrollable": true, "scrollTop": 700, "maxScrollTop": 2100, '
        '"atTop": false, "atBottom": false, "progress": 0.333}\n'
        "### Ran Playwright code\n```js\nawait page.evaluate(...)\n```"
    )
    state = _node(raw)._get_page_scroll_state({})
    assert state["scrollTop"] == 700
    assert state["atBottom"] is False


def test_unparseable_reply_raises():
    with pytest.raises(RuntimeError):
        _node("no json here")._get_page_scroll_state({})
