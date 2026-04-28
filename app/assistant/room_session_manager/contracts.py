from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class InboundEnvelope:
    surface: str
    room_id: str
    context_id: str
    request_id: str
    speaker_name: str
    speaker_id: str
    speaker_external_id: Optional[str]
    content: str
    timestamp_local: str
    inbound_line: str
    transport_message_id: str
    transport_from: str
    transport_to: str
    reply_to: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OutboundIntent:
    request_id: str
    room_id: str
    room_surface: str
    room_context_id: str
    reply_text: str
    send: bool
    delivery_mode: str
    reply_to: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
