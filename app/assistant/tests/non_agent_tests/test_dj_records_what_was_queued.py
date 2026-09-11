"""The DJ records the song the FRONTEND queued, not the one the backend picked (2026-09-10).

The player walks the candidate list and queues the first one Apple Music can find, then
emits `music_song_queued`. The backend used to record at PICK time and only log the
confirmation, so when the player substituted ("Highlands" picked, "Sparkling Adventure"
queued) the history held a song that never played and missed the one that did — which the
no-repeat filter then could not see. Hermetic: no socket, no LLM, no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.dj_manager import manager as M
from app.assistant.dj_manager.events import FrontendQueued


class _FakeSocket:
    def send_command(self, *a, **k):
        return True


class _FakeVibe:
    def get_targets(self):
        return {"audio_targets": {"energy": 63.0, "valence": 54.0}}


class _FakeSelector:
    def pop_backup(self):
        return SimpleNamespace(title="Backup Song", artist="Backup Artist",
                               search_query="Backup Song by Backup Artist", reasoning="", score=0.5)

    def clear_backups(self):
        pass

    def backup_count(self):
        return 0


@pytest.fixture
def dj(monkeypatch):
    recorded = []

    def fake_record(*, title, artist, search_query=None, audio_targets=None):
        recorded.append({"title": title, "artist": artist, "query": search_query, "targets": audio_targets})

    import app.models.played_songs as ps
    monkeypatch.setattr(ps, "record_song_play", fake_record)
    m = M.DJManager(socket_client=_FakeSocket(), vibe=_FakeVibe(), selector=_FakeSelector())
    return m, recorded


def test_confirmation_records_the_dataset_names_with_the_last_pick_targets(dj):
    m, recorded = dj
    m._last_pick_targets = {"audio_targets": {"energy": 63.0, "valence": 54.0}}
    m._handle_event(FrontendQueued(data={
        "title": "Sparkling Adventure (Remastered)", "artist": "Tall Black Guy",
        "dataset_title": "Sparkling Adventure", "dataset_artist": "Tall Black Guy",
        "track_id": "00f3VeR3XTQYSsqD8FX4ZG", "query": "Sparkling Adventure by Tall Black Guy",
    }))
    assert recorded == [{
        "title": "Sparkling Adventure", "artist": "Tall Black Guy",
        "query": "Sparkling Adventure by Tall Black Guy",
        "targets": {"energy": 63.0, "valence": 54.0},
    }]


def test_confirmation_without_dataset_names_falls_back_to_display_names(dj):
    m, recorded = dj
    m._handle_event(FrontendQueued(data={"title": "Highlands", "artist": "madmax", "query": "Highlands by madmax"}))
    assert recorded[0]["title"] == "Highlands" and recorded[0]["artist"] == "madmax"


def test_backup_pop_records_nothing_but_keeps_the_targets_for_the_confirmation(dj):
    m, recorded = dj
    out = m._get_backup_song_internal()
    assert out["title"] == "Backup Song"
    assert recorded == []
    assert m._last_pick_targets == {"audio_targets": {"energy": 63.0, "valence": 54.0}}


def test_no_repeat_window_is_a_week():
    assert M.NO_REPEAT_WINDOW_HOURS == 168.0
