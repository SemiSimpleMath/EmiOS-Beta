from datetime import datetime, timezone

from app.assistant.afk_manager.afk_statistics import get_afk_intervals_overlapping_range


class _Seg:
    def __init__(self, start_time, end_time):
        self.start_time = start_time
        self.end_time = end_time


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_no_segments_returns_single_gap(monkeypatch):
    def _fake_get_segments(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "app.assistant.afk_manager.afk_statistics.get_active_segments_overlapping_range",
        _fake_get_segments,
    )

    gaps = get_afk_intervals_overlapping_range(
        start_utc=_dt("2026-01-22T00:00:00"),
        end_utc=_dt("2026-01-22T02:00:00"),
    )
    assert len(gaps) == 1
    assert gaps[0]["start_utc"].startswith("2026-01-22T00:00:00")
    assert gaps[0]["end_utc"].startswith("2026-01-22T02:00:00")


def test_single_segment_inside_range_two_gaps(monkeypatch):
    def _fake_get_segments(*args, **kwargs):
        return [
            _Seg(_dt("2026-01-22T00:30:00"), _dt("2026-01-22T01:00:00")),
        ]

    monkeypatch.setattr(
        "app.assistant.afk_manager.afk_statistics.get_active_segments_overlapping_range",
        _fake_get_segments,
    )

    gaps = get_afk_intervals_overlapping_range(
        start_utc=_dt("2026-01-22T00:00:00"),
        end_utc=_dt("2026-01-22T02:00:00"),
    )
    assert len(gaps) == 2
    assert gaps[0]["start_utc"].startswith("2026-01-22T00:00:00")
    assert gaps[0]["end_utc"].startswith("2026-01-22T00:30:00")
    assert gaps[1]["start_utc"].startswith("2026-01-22T01:00:00")
    assert gaps[1]["end_utc"].startswith("2026-01-22T02:00:00")


def test_adjacent_segments_no_gap_between(monkeypatch):
    def _fake_get_segments(*args, **kwargs):
        return [
            _Seg(_dt("2026-01-22T00:00:00"), _dt("2026-01-22T01:00:00")),
            _Seg(_dt("2026-01-22T01:00:00"), _dt("2026-01-22T02:00:00")),
        ]

    monkeypatch.setattr(
        "app.assistant.afk_manager.afk_statistics.get_active_segments_overlapping_range",
        _fake_get_segments,
    )

    gaps = get_afk_intervals_overlapping_range(
        start_utc=_dt("2026-01-22T00:00:00"),
        end_utc=_dt("2026-01-22T02:00:00"),
    )
    assert len(gaps) == 0


def test_segment_crossing_range_returns_overlapping_gap(monkeypatch):
    def _fake_get_segments(*args, **kwargs):
        return [
            _Seg(_dt("2026-01-22T00:30:00"), _dt("2026-01-22T03:00:00")),
        ]

    monkeypatch.setattr(
        "app.assistant.afk_manager.afk_statistics.get_active_segments_overlapping_range",
        _fake_get_segments,
    )

    gaps = get_afk_intervals_overlapping_range(
        start_utc=_dt("2026-01-22T00:00:00"),
        end_utc=_dt("2026-01-22T02:00:00"),
    )
    # Only gap before the segment; no clipping inside segment
    assert len(gaps) == 1
    assert gaps[0]["start_utc"].startswith("2026-01-22T00:00:00")
    assert gaps[0]["end_utc"].startswith("2026-01-22T00:30:00")
