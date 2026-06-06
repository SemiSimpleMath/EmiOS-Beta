"""ID-anchored Bluesky core — proves the cross-wire bug is structurally impossible.

Reproduces the real incident: two posts by the SAME author in one timeline —
  A: "...finished an open source event by dancing to Dancing Queen..."  (no image)
  B: "Such a big deal. I was excited to be in the room..."              (image, quotes award graphic)
The planner drew the reply content from A but attached it to B's URI. With
ref-anchoring, the reply record is built from the SELECTED ref, so content and
target cannot diverge.

Pure unit tests — no network, no DI. Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/tool_tests/test_bluesky_id_anchored.py
"""
from __future__ import annotations

import pytest

from app.assistant.lib.core_tools.bluesky.bluesky_core import (
    build_like_record,
    build_post_record,
    build_reply_record,
    compact_timeline,
    extract_image,
    target_from_ref,
)

# at:// URIs + cids modeled on the real incident (distinct per post, same author).
POST_A_URI = "at://did:plc:werd/app.bsky.feed.post/dancingqueen"   # the Dancing Queen post
POST_A_CID = "bafyA"
POST_B_URI = "at://did:plc:werd/app.bsky.feed.post/awardsroom"     # the "excited to be in the room" post
POST_B_CID = "bafyB"
ROOT_URI = "at://did:plc:other/app.bsky.feed.post/threadroot"
ROOT_CID = "bafyRoot"


def _feed():
    """A getTimeline-shaped response with the two same-author posts plus a reply."""
    return {
        "feed": [
            {  # A — the post the reply content actually belongs to
                "post": {
                    "uri": POST_A_URI,
                    "cid": POST_A_CID,
                    "author": {"handle": "werd.io", "displayName": "Ben Werdmuller"},
                    "record": {
                        "$type": "app.bsky.feed.post",
                        "text": "Well, that was the first time I finished an open source / "
                                "liberatory tech event by dancing to Dancing Queen. Into it.",
                    },
                }
            },
            {  # B — same author, different post, carries an image + quotes the award graphic
                "post": {
                    "uri": POST_B_URI,
                    "cid": POST_B_CID,
                    "author": {"handle": "werd.io", "displayName": "Ben Werdmuller"},
                    "record": {
                        "$type": "app.bsky.feed.post",
                        "text": "Such a big deal. I was excited to be in the room. Congratulations to all.",
                    },
                    "embed": {
                        "$type": "app.bsky.embed.recordWithMedia#view",
                        "media": {
                            "$type": "app.bsky.embed.images#view",
                            "images": [{
                                "fullsize": "https://cdn.example/awards.jpg",
                                "thumb": "https://cdn.example/awards_thumb.jpg",
                                "alt": "A graphic announcing the Open Social Awards 2026 Winners.",
                            }],
                        },
                        "record": {"record": {"value": {"text": "Winners of the first ever Open Social Awards!"}}},
                    },
                }
            },
            {  # C — a reply, to exercise thread-root resolution
                "post": {
                    "uri": "at://did:plc:x/app.bsky.feed.post/replychild",
                    "cid": "bafyC",
                    "author": {"handle": "someone.bsky.social"},
                    "record": {
                        "$type": "app.bsky.feed.post",
                        "text": "agreed, totally.",
                        "reply": {
                            "root": {"uri": ROOT_URI, "cid": ROOT_CID},
                            "parent": {"uri": "at://did:plc:x/app.bsky.feed.post/replyparent", "cid": "bafyP"},
                        },
                    },
                }
            },
        ]
    }


def test_compact_timeline_assigns_stable_refs_to_distinct_posts():
    rendered, ref_map = compact_timeline(_feed())
    assert set(ref_map) == {"b1", "b2", "b3"}
    # The two same-author posts get DIFFERENT refs and DIFFERENT uris.
    assert ref_map["b1"]["uri"] == POST_A_URI
    assert ref_map["b2"]["uri"] == POST_B_URI
    assert ref_map["b1"]["uri"] != ref_map["b2"]["uri"]
    assert "Dancing Queen" in ref_map["b1"]["text"]
    assert "excited to be in the room" in ref_map["b2"]["text"]
    # The compact list shows both — the planner has the real text to choose from.
    assert "[b1]" in rendered and "[b2]" in rendered
    assert "dancing to Dancing Queen" in rendered


def test_reply_binds_to_selected_ref_not_a_sibling():
    """THE anti-cross-wire proof: reply content for the dancing post (b1) builds a
    record whose parent is b1's uri — it is impossible to land it on b2."""
    _, ref_map = compact_timeline(_feed())
    target = target_from_ref(ref_map, "b1")
    record = build_reply_record(
        "perfect ending honestly. dancing queen is undefeated",
        target,
        created_at="2026-06-05T23:53:00.000Z",
    )
    assert record["reply"]["parent"]["uri"] == POST_A_URI
    assert record["reply"]["parent"]["cid"] == POST_A_CID
    # Crucially NOT the awards post:
    assert record["reply"]["parent"]["uri"] != POST_B_URI
    # A top-level post is its own thread root.
    assert record["reply"]["root"]["uri"] == POST_A_URI


def test_reply_to_a_reply_uses_thread_root_not_parent():
    _, ref_map = compact_timeline(_feed())
    record = build_reply_record("nice", target_from_ref(ref_map, "b3"), created_at="2026-06-05T00:00:00.000Z")
    # parent = the post we answered; root = the thread origin it carried.
    assert record["reply"]["parent"]["uri"] == "at://did:plc:x/app.bsky.feed.post/replychild"
    assert record["reply"]["root"]["uri"] == ROOT_URI
    assert record["reply"]["root"]["cid"] == ROOT_CID


def test_like_binds_to_selected_ref():
    _, ref_map = compact_timeline(_feed())
    rec = build_like_record(target_from_ref(ref_map, "b2"), created_at="2026-06-05T00:00:00.000Z")
    assert rec["subject"]["uri"] == POST_B_URI
    assert rec["subject"]["cid"] == POST_B_CID


def test_image_detected_on_b_not_a_and_surfaced_in_compact_line():
    rendered, ref_map = compact_timeline(_feed())
    assert ref_map["b1"]["has_image"] is False
    assert ref_map["b2"]["has_image"] is True
    assert ref_map["b2"]["image_url"] == "https://cdn.example/awards.jpg"
    assert "Open Social Awards 2026 Winners" in ref_map["b2"]["image_alt"]
    # The compact line for b2 advertises the image so the planner can hydrate it.
    b2_line = [ln for ln in rendered.splitlines() if ln.startswith("[b2]")][0]
    assert "image:" in b2_line


def test_extract_image_plain_images_embed():
    post = {
        "embed": {
            "$type": "app.bsky.embed.images#view",
            "images": [{"fullsize": "https://cdn.example/x.jpg", "alt": "a cat"}],
        }
    }
    img = extract_image(post)
    assert img == {"url": "https://cdn.example/x.jpg", "alt": "a cat"}


def test_post_record_is_top_level_no_reply():
    rec = build_post_record("Friday brain off switch found.", created_at="2026-06-05T23:51:48.000Z")
    assert rec["$type"] == "app.bsky.feed.post"
    assert rec["text"] == "Friday brain off switch found."
    assert "reply" not in rec  # original post has no target


def test_unknown_ref_fails_loud():
    _, ref_map = compact_timeline(_feed())
    with pytest.raises(KeyError):
        target_from_ref(ref_map, "b99")
    with pytest.raises(KeyError):
        target_from_ref(None, "b1")
