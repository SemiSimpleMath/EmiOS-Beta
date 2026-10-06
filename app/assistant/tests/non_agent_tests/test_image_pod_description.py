"""Every image pod is described when it is created (2026-10-05).

The chat route started the captioner on a thread call that lacked a required argument, so it never
ran: every chat image since April stayed "[image: png, 58kb, 500x248]". Pins: an image minted
without a description is queued for the captioner and one minted with one is not; the captioner
writes its description back and raises on an empty answer; readers see a chat message's image as its
description. Fake pod store, scratch directory, fake captioner. Invented data only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from app.assistant.pod_store import image_ingest, image_pod_enrichment as E


class FakeStore:
    def __init__(self):
        self.pods = {}

    def get(self, pod_id):
        return self.pods.get(pod_id)

    def put(self, pod):
        self.pods[pod.pod_id] = pod


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    store, queued = FakeStore(), []
    monkeypatch.setattr(image_ingest, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(E, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(E, "PodStore", lambda: store)
    monkeypatch.setattr(E, "queue_description", queued.append)
    src = tmp_path / "upload.png"
    Image.new("RGB", (8, 6), "red").save(src)
    return SimpleNamespace(store=store, queued=queued, src=src)


def test_an_image_without_a_description_is_queued_and_one_with_it_is_not(scratch, tmp_path):
    pod = image_ingest.ingest_image_file(src_path=scratch.src, source_kind="chat_attachment", pod_store=scratch.store,
                                         metadata_extra={"chat_trigger_text": "our new rug", "chat_room_id": "r1"})
    assert scratch.queued == [pod.pod_id] and pod.metadata["vision_extraction_status"] == "pending"
    other = tmp_path / "frame.png"
    Image.new("RGB", (8, 6), "blue").save(other)
    seen = image_ingest.ingest_image_file(src_path=other, source_kind="ring_doorbell_event", pod_store=scratch.store,
                                          one_liner="A parcel on the step", body="A brown parcel on the front step.")
    assert scratch.queued == [pod.pod_id]                                 # not queued again
    assert (seen.one_liner, seen.body, seen.metadata["vision_extraction_status"]) == (
        "A parcel on the step", "A brown parcel on the front step.", "done")
    with pytest.raises(ValueError, match="both"):
        image_ingest.ingest_image_file(src_path=scratch.src, pod_store=scratch.store, body="only a body")


def test_the_captioner_reads_the_message_and_writes_its_description_back(scratch, monkeypatch):
    pod = image_ingest.ingest_image_file(src_path=scratch.src, source_kind="chat_attachment", pod_store=scratch.store,
                                         metadata_extra={"chat_trigger_text": "our new rug", "chat_room_id": "r1"})
    monkeypatch.setattr(E, "_chat_context", lambda room, at: f"- 10:00 Owner: look at this ({room})")
    monkeypatch.setattr(E, "_local_now", lambda: "Mon 2026-03-09 10:01")
    asked = []
    answer = {"one_liner": "A red rug in the hall", "body": "A plain red rug on a wooden floor; the owner's new rug.",
              "tags": ["Rug"], "depicted_entities": []}
    out = E.describe_image_pod(pod.pod_id, call=lambda i: asked.append(i) or answer)
    assert asked[0]["trigger_message"] == "our new rug" and "(r1)" in asked[0]["chat_context_block"]
    stored = scratch.store.get(pod.pod_id)
    assert (stored.one_liner, stored.tags, stored.metadata["vision_extraction_status"]) == (
        "A red rug in the hall", ["rug"], "done")
    assert out["status"] == "described"
    assert E.describe_image_pod(pod.pod_id, call=lambda i: 1 / 0)["status"] == "already_described"


def test_an_empty_caption_raises_and_leaves_the_pod_pending(scratch):
    pod = image_ingest.ingest_image_file(src_path=scratch.src, pod_store=scratch.store)
    with pytest.raises(ValueError, match="no description"):
        E.describe_image_pod(pod.pod_id, call=lambda i: {"one_liner": "", "body": ""})
    assert scratch.store.get(pod.pod_id).metadata["vision_extraction_status"] == "pending"
    asked = []
    E.describe_image_pod(pod.pod_id, call=lambda i: asked.append(i) or {"one_liner": "x", "body": "y"})
    assert asked[0]["chat_context_block"] == "(the image was not sent in a chat)"


def test_a_message_shows_its_image_as_the_description(scratch):
    pod = image_ingest.ingest_image_file(src_path=scratch.src, pod_store=scratch.store)
    text = f"check this out:\n{pod.pod_id}"
    assert E.with_images(text) == f"check this out:\n[image {pod.pod_id}: not described yet]"
    E.describe_image_pod(pod.pod_id, call=lambda i: {"one_liner": "A red square", "body": "Solid red."})
    assert E.with_images(text) == "check this out:\n[image: A red square. Solid red.]"
    assert E.with_images("datapod:image:gone") == "[image datapod:image:gone: no longer stored]"
