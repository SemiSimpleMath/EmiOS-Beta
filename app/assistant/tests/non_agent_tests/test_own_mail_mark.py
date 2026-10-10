"""The assistant's own mail is known as its own when it comes back (2026-10-10).

The assistant sends from the owner's Gmail, so an email it sends to the owner lands in the owner's inbox with the owner
as sender; on 2026-10-10 the brain read its forward as "<owner> forwarded the Yahoo message". Pins: send_email marks
every email it sends, with the work and task it was sent for on a work session; the fetch reads the mark only on an
email from one of the accounts the assistant holds, the owner's or its own, however many; every reader's sender line
and the pod's one-liner then say the assistant sent it. Fake Gmail service; invented data only.
"""
from __future__ import annotations

import base64
import threading
from email import message_from_bytes

from app.assistant.lib.core_tools.email_tool.utils.email_utils import sent_by_assistant
from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient
from app.assistant.lib.tools.send_email.send_email import _own_mail_headers
from app.assistant.pod_store.email_pods import build_email_pod, email_sender_line


class _Send:
    def __init__(self, sink):
        self.sink = sink

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sink.append(body["raw"])
        return self

    def execute(self):
        return {"id": "m1"}


HELD = {"owner@mail.example", "owner.work@mail.example", "assistant@mail.example"}


def _sent(headers, sender="owner@mail.example"):
    """The email send_email would put on the wire with these headers, parsed back, as Gmail delivers it from `sender`."""
    sink = []
    client = object.__new__(GmailAPIClient)
    client.service = _Send(sink)
    assert client.send_email("owner@mail.example", "Fwd: Hi", "Forwarding.", headers=headers) == {"id": "m1"}
    message = message_from_bytes(base64.urlsafe_b64decode(sink[0]))
    message["From"] = f"Some Name <{sender}>"
    return message


def _in_work_session(fn):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), name="work_session::work_abc::forward--abc")
    t.start()
    t.join()
    return out[0]


def test_an_email_sent_off_a_work_session_carries_only_the_mark():
    message = _sent(_own_mail_headers())
    assert sent_by_assistant(message, HELD) == {
        "sent_by_assistant": True, "sent_for_work": None, "sent_for_task": None}


def test_an_email_sent_for_work_names_the_work_and_task():
    message = _sent(_in_work_session(_own_mail_headers))
    assert sent_by_assistant(message, HELD) == {
        "sent_by_assistant": True, "sent_for_work": "work_abc", "sent_for_task": "forward--abc"}


def test_the_mark_counts_from_every_held_account_and_no_other():
    for sender in ("owner.work@mail.example", "Assistant@Mail.example"):
        assert sent_by_assistant(_sent(_own_mail_headers(), sender), HELD)["sent_by_assistant"] is True
    assert sent_by_assistant(_sent(_own_mail_headers(), "stranger@else.example"), HELD) == {}
    assert sent_by_assistant(_sent({}), HELD) == {}


def _record(**kw):
    return {"uid": "u1", "account_id": "acct", "subject": "Fwd: Hi", "sender": "Owner Name",
            "email_address": "owner@mail.example", "importance": 7, "body": "Forwarding.", **kw}


def test_readers_see_the_assistant_as_the_sender():
    mine = _record(sent_by_assistant=True, sent_for_work="work_abc", sent_for_task="forward--abc")
    assert email_sender_line(mine) == "the assistant, sending from Owner Name <owner@mail.example>"
    assert build_email_pod(mine).one_liner == "the assistant, sending from Owner Name <owner@mail.example>: Fwd: Hi"
    assert build_email_pod(mine).metadata["sent_for_work"] == "work_abc"
    assert email_sender_line(_record()) == "Owner Name <owner@mail.example>"
    assert build_email_pod(_record()).one_liner == "Owner Name: Fwd: Hi"
