from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


class DJEvent:
    pass


@dataclass(frozen=True)
class Enable(DJEvent):
    continuous_mode: bool = True


@dataclass(frozen=True)
class Disable(DJEvent):
    pass


@dataclass(frozen=True)
class SetContinuousMode(DJEvent):
    enabled: bool


@dataclass(frozen=True)
class SetPauseOnAfk(DJEvent):
    enabled: bool


@dataclass(frozen=True)
class AfkStateChanged(DJEvent):
    is_afk: bool


@dataclass(frozen=True)
class TrackChanged(DJEvent):
    track: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class RequestPickAndQueue(DJEvent):
    reason: str = "manual"


@dataclass(frozen=True)
class PickSong(DJEvent):
    """
    Request the DJ thread to pick a song (selection only).

    The DJ thread will put the pick result (dict or None) into reply_queue.
    """

    reason: str = "manual"
    reply_queue: Any = None
    # Allow a one-shot pick even when DJ mode is disabled.
    allow_when_disabled: bool = False


@dataclass(frozen=True)
class RequestBackupSong(DJEvent):
    """
    Request the DJ thread to pop and return the next backup song.

    The DJ thread will put the backup result (dict or None) into reply_queue.
    """

    reply_queue: Any = None


@dataclass(frozen=True)
class FrontendQueued(DJEvent):
    data: Dict[str, Any]


@dataclass(frozen=True)
class StopThread(DJEvent):
    pass
