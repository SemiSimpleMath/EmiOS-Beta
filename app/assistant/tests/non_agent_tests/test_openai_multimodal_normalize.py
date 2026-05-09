import base64
from pathlib import Path

from app.services.llm_client import _normalize_openai_responses_messages


def test_normalize_keeps_plain_text_messages():
    msgs = [{"role": "user", "content": "hello"}]
    out = _normalize_openai_responses_messages(msgs)
    assert out == msgs


def test_normalize_converts_text_blocks():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    out = _normalize_openai_responses_messages(msgs)
    assert out[0]["content"][0]["type"] == "input_text"
    assert out[0]["content"][0]["text"] == "hi"


def test_normalize_converts_image_url_blocks():
    msgs = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
        }
    ]
    out = _normalize_openai_responses_messages(msgs)
    assert out[0]["content"][0]["type"] == "input_image"
    assert out[0]["content"][0]["image_url"] == "https://example.com/a.png"


def test_normalize_converts_image_path_blocks(tmp_path: Path):
    # Write a tiny "fake png" payload; we only validate that it becomes a data URI.
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    msgs = [{"role": "user", "content": [{"type": "image_path", "path": str(p)}]}]
    out = _normalize_openai_responses_messages(msgs)
    assert out[0]["content"][0]["type"] == "input_image"
    assert out[0]["content"][0]["image_url"].startswith("data:image/png;base64,")


def test_normalize_converts_image_base64_blocks():
    b64 = base64.b64encode(b"abc").decode("ascii")
    msgs = [{"role": "user", "content": [{"type": "image_base64", "data": b64, "mime": "image/png"}]}]
    out = _normalize_openai_responses_messages(msgs)
    assert out[0]["content"][0]["type"] == "input_image"
    assert out[0]["content"][0]["image_url"] == f"data:image/png;base64,{b64}"

