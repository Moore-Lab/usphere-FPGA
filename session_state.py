"""
session_state.py

Unified persistent GUI state — auto-saved every 5 s and on close.
Replaces the per-tab trapping_presets.json and the fragment stored in
fpga_session_log.jsonl.

Format (session_state.json)
---------------------------
{
  "config":    { bitfile, resource, poll_interval_ms },
  "host_params": { freq_x, Q_x, ... },
  "registers": { reg_name: value, ... },   # restored after FPGA connect
  "dropper":   { retrieval_mm, dropping_mm, retraction_mm, ... },
  "shaker":    { start_v, step_v, n_steps, ... },
  "trapping":  { prepare: {...}, feedback: {...}, ... }
}
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / "session_state.json"


def load_state() -> dict:
    """Return the persisted session dict, or {} if missing / corrupt."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    """Overwrite session_state.json atomically (write-then-rename)."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
