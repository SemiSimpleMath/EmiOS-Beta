"""A scratch concerns register for tests: the real concerns table in a temporary sqlite file.

    reg = ScratchRegister(tmp_path)
    reg.write({"active": [...]})                 # seed
    persist.apply_noticer_output(out, connect=reg.connect, tick_log_path=...)
    reg.read()["dormant"]                        # inspect
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.assistant.subconscious import concern_store
from belief_engine.intake.store import sqlite_file


class ScratchRegister:
    def __init__(self, tmp_path: Path, name: str = "concerns.db"):
        self.connect = sqlite_file(Path(tmp_path) / name)
        self.tick_log = Path(tmp_path) / "ticks.jsonl"

    def write(self, register: Dict[str, Any]) -> "ScratchRegister":
        concern_store.save_register(register, connect=self.connect)
        return self

    def read(self) -> Dict[str, Any]:
        return concern_store.load_register(connect=self.connect)
