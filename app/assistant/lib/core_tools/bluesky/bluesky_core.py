"""Pure (no-network, no-DI) helpers for ID-anchored Bluesky actions.

The cross-wire bug (the assistant replied "dancing queen is undefeated" — content drawn
from post A — but attached it to post B, a *different* same-author post) happened
because the planner re-typed atproto URIs by hand out of a 53 KB timeline blob.
These helpers make that class of error impossible:

  - ``compact_timeline`` renders a getTimeline response to a short list of stable
    refs (``b1``, ``b2``, …) and a ref-map ``{ref -> {uri, cid, root_*, …}}``.
  - ``build_reply_record`` / ``build_like_record`` build the atproto record from a
    SELECTED ref's canonical fields. The model picks ``post_ref="b1"`` and the text
    rides with it — there is no second place to retype a URI, so content and target
    cannot diverge.

This mirrors the Playwright snapshot/ref pattern: ``web_fill_ref`` resolves a
``ref`` against ``playwright_latest_snapshot``; here a ``post_ref`` resolves
against the latest Bluesky timeline ref-map. Kept import-free (stdlib only) so the
whole anti-cross-wire core is unit-testable without network or DI.
"""
from __future__ import annotations

from typing import Optional

# Posts are short (<=300 graphemes) so we show them nearly whole in the compact
# list — the planner needs the real text to pick the right ref. Truncate only the
# rare long one.
_MAX_TEXT = 280
_ALT_SNIPPET = 90
_QUOTE_SNIPPET = 90


def _post_text(post: dict) -> str:
    rec = post.get("record") or {}
    return str(rec.get("text") or "").replace("\n", " ").strip()


def _author_handle(post: dict) -> str:
    author = post.get("author") or {}
    return str(author.get("handle") or "").strip()


def extract_image(post: dict) -> Optional[dict]:
    """Return ``{"url": <fullsize>, "alt": <alt>}`` for the FIRST image on a post,
    or ``None``. Handles both ``app.bsky.embed.images#view`` and
    ``app.bsky.embed.recordWithMedia#view`` (where the image rides in ``media``).
    The quoted-record case (image on the *quoted* post) is intentionally NOT
    treated as this post's image — that was the real bug (the award graphic was on
    a quoted record, not Ben's post)."""
    embed = post.get("embed") or {}
    etype = str(embed.get("$type") or "")
    images = None
    if etype.startswith("app.bsky.embed.images"):
        images = embed.get("images")
    elif etype.startswith("app.bsky.embed.recordWithMedia"):
        media = embed.get("media") or {}
        if str(media.get("$type") or "").startswith("app.bsky.embed.images"):
            images = media.get("images")
    if isinstance(images, list) and images:
        first = images[0] or {}
        url = str(first.get("fullsize") or first.get("thumb") or "").strip()
        if url:
            return {"url": url, "alt": str(first.get("alt") or "").strip()}
    return None


def quoted_text(post: dict) -> Optional[str]:
    """If the post quotes another record, return the quoted post's text (so the
    compact line can show it WITHOUT letting the planner confuse it for this
    post's own text)."""
    embed = post.get("embed") or {}
    etype = str(embed.get("$type") or "")
    rec = None
    if etype.startswith("app.bsky.embed.record"):
        rec = embed.get("record")
    elif etype.startswith("app.bsky.embed.recordWithMedia"):
        inner = embed.get("record") or {}
        rec = inner.get("record") or inner
    if isinstance(rec, dict):
        value = rec.get("value") or rec.get("record") or {}
        text = str(value.get("text") or "").strip()
        if text:
            return text.replace("\n", " ")
    return None


def _root_ref(post: dict) -> tuple[str, str]:
    """The thread-root ``(uri, cid)`` to use when replying to this post.

    atproto replies carry BOTH a ``parent`` (the post you answer) and a ``root``
    (the thread origin). If this post is itself a reply, root = its
    ``record.reply.root``; otherwise the post IS the root. The planner could never
    hand-maintain this correctly — the resolver does it every time."""
    rec = post.get("record") or {}
    reply = rec.get("reply") or {}
    root = reply.get("root") or {}
    root_uri = str(root.get("uri") or "").strip()
    root_cid = str(root.get("cid") or "").strip()
    if root_uri and root_cid:
        return root_uri, root_cid
    return str(post.get("uri") or "").strip(), str(post.get("cid") or "").strip()


def compact_timeline(
    feed_response: dict, *, max_posts: int = 20, own_handle: Optional[str] = None
) -> tuple[str, dict]:
    """Render a getTimeline response into ``(compact_text, ref_map)``.

    ``ref_map``: ``{"b1": {uri, cid, root_uri, root_cid, author, text, has_image,
    image_url, image_alt}}`` — the canonical fields the action tools resolve against.
    ``compact_text``: one ``[bN] @handle · "text" [markers]`` line per post; the
    only thing the planner sees. Duplicate/repost URIs collapse to the first ref.

    ``own_handle``: the acting account's handle. The home feed includes your OWN
    posts; offering them as engagement targets let the planner reply to itself
    (it did — a public self-reply). They are excluded here so it is structurally
    impossible, with a count surfaced rather than silently dropped.
    """
    feed = feed_response.get("feed") if isinstance(feed_response, dict) else None
    if not isinstance(feed, list):
        feed = []
    own = (own_handle or "").strip().lower()

    lines: list[str] = []
    ref_map: dict[str, dict] = {}
    seen_uris: set[str] = set()
    hidden_own = 0
    n = 0
    for item in feed:
        if n >= max_posts:
            break
        post = (item or {}).get("post") if isinstance(item, dict) else None
        if not isinstance(post, dict):
            continue
        uri = str(post.get("uri") or "").strip()
        cid = str(post.get("cid") or "").strip()
        if not uri or not cid or uri in seen_uris:
            continue
        seen_uris.add(uri)

        handle = _author_handle(post)
        if own and handle.lower() == own:
            hidden_own += 1  # never offer your own posts as engagement targets
            continue
        n += 1
        ref = f"b{n}"
        text = _post_text(post)
        img = extract_image(post)
        root_uri, root_cid = _root_ref(post)

        ref_map[ref] = {
            "uri": uri,
            "cid": cid,
            "root_uri": root_uri,
            "root_cid": root_cid,
            "author": handle,
            "text": text,
            "has_image": bool(img),
            "image_url": img["url"] if img else None,
            "image_alt": img["alt"] if img else None,
        }

        snippet = text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"
        markers: list[str] = []
        if img:
            alt = img["alt"]
            markers.append(f'image: "{alt[:_ALT_SNIPPET]}"' if alt else "image (no alt)")
        quote = quoted_text(post)
        if quote:
            markers.append(f'quotes: "{quote[:_QUOTE_SNIPPET]}"')
        mark = f"  [{'; '.join(markers)}]" if markers else ""
        lines.append(f'[{ref}] @{handle} · "{snippet}"{mark}')

    own_note = f" ({hidden_own} of your own posts hidden)" if hidden_own else ""
    header = (
        f"Bluesky timeline — {len(ref_map)} posts{own_note}. "
        "To like or reply, pass the post's ref (e.g. bluesky_reply post_ref=b1). "
        "To see a post's image before replying, bluesky_hydrate_post post_ref=b1."
    )
    body = "\n".join(lines) if lines else "(no posts)"
    return f"{header}\n{body}", ref_map


def target_from_ref(ref_map: Optional[dict], ref: str) -> dict:
    """Resolve a planner-chosen ``post_ref`` to its canonical fields. Fail loud on
    an unknown ref — never guess a target."""
    ref = str(ref or "").strip()
    if not isinstance(ref_map, dict) or ref not in ref_map:
        raise KeyError(
            f"unknown post_ref {ref!r}; call bluesky_timeline first to get the current refs"
        )
    return ref_map[ref]


def build_reply_record(text: str, target: dict, *, created_at: str) -> dict:
    """Build an ``app.bsky.feed.post`` reply record bound to ``target``'s ref.

    ``parent`` = the selected post; ``root`` = its thread origin. Because ``target``
    came from a single chosen ref, the reply text cannot land on a different post.
    """
    return {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": created_at,
        "reply": {
            "root": {"uri": target["root_uri"], "cid": target["root_cid"]},
            "parent": {"uri": target["uri"], "cid": target["cid"]},
        },
    }


def build_like_record(target: dict, *, created_at: str) -> dict:
    """Build an ``app.bsky.feed.like`` record bound to ``target``'s ref."""
    return {
        "$type": "app.bsky.feed.like",
        "subject": {"uri": target["uri"], "cid": target["cid"]},
        "createdAt": created_at,
    }


def build_post_record(text: str, *, created_at: str) -> dict:
    """Build an original (top-level) ``app.bsky.feed.post`` record. No ref — an
    original post has no target, so there is nothing to cross-wire."""
    return {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": created_at,
    }
