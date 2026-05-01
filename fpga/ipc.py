"""
fpga_ipc.py

Inter-process communication between the FPGA control program and consumers
(usphere-DAQ, offline analysis scripts).

NOTE — Long-term architecture
------------------------------
The file-based IPC approach here (tic_state.json, shake_events.jsonl) is an
interim solution chosen for simplicity and zero additional dependencies.

The intended long-term replacement is an InfluxDB time-series database:
  - All environmental variables (TIC pressure, vacuum gauge readings, any
    future slow-monitoring quantities) would be written to InfluxDB as
    tagged measurements on each poll cycle.
  - The DAQ script would query InfluxDB at the start/end of each h5 recording
    to retrieve the relevant time-windowed data, rather than reading flat files.
  - This removes the file-ownership problem entirely: any number of programs
    can write to or read from InfluxDB simultaneously without coordination.
  - Shake events (and future discrete instrument events) could be written as
    InfluxDB annotations or as a dedicated measurement with a boolean field.

When migrating: replace TICPublisher with an InfluxDB write client, replace
read_tic_state() / read_shake_events_in_window() with InfluxDB query helpers,
and update ipc/README.md with the new query patterns for the DAQ developer.

Two publishers, each writing to the ipc/ subdirectory:

  TICPublisher
      Overwrites  ipc/tic_state.json   on every TIC poll.
      Consumers read the latest pressure values; stale by at most one poll
      interval (~2 s), which is fine for slow environmental quantities.

  ShakeEventLogger
      Appends one line to  ipc/shake_events.jsonl  on every shake start/stop.
      The file is an ordered, append-only record; consumers filter by UTC
      timestamp to find events within a recording window.
      Pruning: if the file exceeds _MAX_BYTES (3 MB), the oldest line is
      dropped before each new event is written, keeping the file bounded
      while preserving the most recent history.

File formats
------------
tic_state.json — single JSON object, overwritten each cycle:
    {
        "wrg_mbar":  <float | null>,
        "apgx_mbar": <float | null>,
        "ts_utc":    <float>          # time.time()
    }

shake_events.jsonl — one JSON object per line, oldest first:
    {"ts_utc": <float>, "kind": "start"|"stop",
     "amplitude_vpp": <float>, "step": <int>}

Reading shake events for an h5 recording window
------------------------------------------------
    from fpga.ipc import read_shake_events_in_window
    events = read_shake_events_in_window(t_start_utc, t_end_utc)
    # returns list[dict], each dict has ts_utc, kind, amplitude_vpp, step
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_IPC_DIR   = Path(__file__).parent / "ipc"
_TIC_FILE  = _IPC_DIR / "tic_state.json"
_SHAKE_FILE = _IPC_DIR / "shake_events.jsonl"

_MAX_BYTES = 3 * 1024 * 1024   # 3 MB pruning threshold for shake log


def _ensure_ipc_dir() -> None:
    _IPC_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# TIC publisher — latest-value overwrite
# ---------------------------------------------------------------------------

class TICPublisher:
    """
    Writes the most recent TIC readings to ipc/tic_state.json.

    Thread-safe: call update() from any thread (e.g. the poll worker).
    """

    def __init__(self) -> None:
        _ensure_ipc_dir()
        self._lock = threading.Lock()

    def update(self, wrg_mbar: float | None,
               apgx_mbar: float | None) -> None:
        """
        Overwrite tic_state.json with the latest readings.

        Pass None (or float('nan')) for a gauge that returned an error.
        """
        def _clean(v):
            if v is None:
                return None
            try:
                return None if v != v else float(v)   # NaN → None
            except (TypeError, ValueError):
                return None

        payload = {
            "wrg_mbar":  _clean(wrg_mbar),
            "apgx_mbar": _clean(apgx_mbar),
            "ts_utc":    time.time(),
        }
        with self._lock:
            _TIC_FILE.write_text(
                json.dumps(payload, indent=None) + "\n",
                encoding="utf-8")


# ---------------------------------------------------------------------------
# Shake event logger — append-only with size-bounded pruning
# ---------------------------------------------------------------------------

class ShakeEventLogger:
    """
    Appends shake start/stop events to ipc/shake_events.jsonl.

    Pruning
    -------
    After each append, if the file exceeds _MAX_BYTES the oldest line is
    removed.  This is O(n) but the file is capped at 3 MB so it is fast.
    The newest events are always preserved; only the oldest are discarded.

    Thread-safe: start() and stop() may be called from any thread.
    """

    def __init__(self) -> None:
        _ensure_ipc_dir()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, amplitude_vpp: float, step: int = 0) -> None:
        """Log a shake-start event."""
        self._append({
            "ts_utc":        time.time(),
            "kind":          "start",
            "amplitude_vpp": float(amplitude_vpp),
            "step":          int(step),
        })

    def stop(self, amplitude_vpp: float = 0.0, step: int = 0) -> None:
        """Log a shake-stop event."""
        self._append({
            "ts_utc":        time.time(),
            "kind":          "stop",
            "amplitude_vpp": float(amplitude_vpp),
            "step":          int(step),
        })

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            with _SHAKE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
            self._prune_if_needed()

    def _prune_if_needed(self) -> None:
        """Drop the oldest line if the file exceeds _MAX_BYTES."""
        if not _SHAKE_FILE.exists():
            return
        while _SHAKE_FILE.stat().st_size > _MAX_BYTES:
            lines = _SHAKE_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
            # Drop the first non-empty line
            trimmed = [l for l in lines[1:] if l.strip()]
            if not trimmed:
                _SHAKE_FILE.write_text("", encoding="utf-8")
                break
            _SHAKE_FILE.write_text("".join(trimmed), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reader helpers — for DAQ and offline scripts
# ---------------------------------------------------------------------------

def read_tic_state() -> dict | None:
    """
    Return the latest TIC state dict, or None if the file does not exist.

    Keys: wrg_mbar (float|None), apgx_mbar (float|None), ts_utc (float).
    """
    try:
        return json.loads(_TIC_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_shake_events_in_window(t_start_utc: float,
                                t_end_utc: float) -> list[dict]:
    """
    Return all shake events whose ts_utc falls within [t_start_utc, t_end_utc].

    Suitable for the DAQ to call when closing an h5 file:
        events = read_shake_events_in_window(file_open_time, file_close_time)

    Returns an empty list if the file does not exist or contains no matches.
    """
    if not _SHAKE_FILE.exists():
        return []
    results = []
    for line in _SHAKE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            ts = ev.get("ts_utc", 0.0)
            if t_start_utc <= ts <= t_end_utc:
                results.append(ev)
        except json.JSONDecodeError:
            continue
    return results


def read_all_shake_events() -> list[dict]:
    """Return every shake event in the log, oldest first."""
    return read_shake_events_in_window(0.0, float("inf"))
