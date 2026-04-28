from pydantic import BaseModel
from typing import Optional, Dict, Any


class BaseEventData(BaseModel):
    """
    Canonical structure for all scheduler events.
    Used across scheduling, execution, and persistence.
    """

    event_id: str                      # Unique identifier
    event_type: str                    # Type of event (e.g. 'one_time_event', 'interval', etc.)
    interval: Optional[int] = None     # Seconds between runs (if interval)
    start_date: Optional[str] = None   # ISO datetime string
    end_date: Optional[str] = None     # ISO datetime string
    jitter: Optional[int] = None       # Seconds of random delay
    event_payload: Dict[str, Any]      # Opaque data to be passed to handler
