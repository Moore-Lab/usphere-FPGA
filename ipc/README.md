# IPC Data Files — Integration Guide for usphere-DAQ

The FPGA control program (usphere-FPGA) writes two files to this directory
while it is running.  The DAQ script should read these files when opening or
closing an h5 recording to capture environmental state and discrete events that
originate from instruments owned by the FPGA program.

---

## Files

### `tic_state.json` — latest TIC pressure readings

Overwritten by the FPGA program on every TIC poll cycle (default interval: 2 s).
Always contains the most recent reading; older values are discarded.

```json
{
    "wrg_mbar":  3.14e-05,
    "apgx_mbar": 8.20e-04,
    "ts_utc":    1745000100.123
}
```

| Field | Type | Notes |
|-------|------|-------|
| `wrg_mbar` | float \| null | Wide-range gauge pressure in mbar. `null` = read error. |
| `apgx_mbar` | float \| null | Pirani APGX gauge pressure in mbar. `null` = read error. |
| `ts_utc` | float | `time.time()` when the reading was taken. |

**How to use in the DAQ:**
Read this file when closing each h5 file and store the values as attributes or
a scalar dataset.  A 2 s staleness is acceptable for slow environmental
quantities like pressure.

```python
import json, pathlib

def read_tic_pressure(ipc_dir):
    path = pathlib.Path(ipc_dir) / "tic_state.json"
    try:
        d = json.loads(path.read_text())
        return d.get("wrg_mbar"), d.get("apgx_mbar"), d.get("ts_utc")
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None, None

wrg, apgx, ts = read_tic_pressure("/path/to/usphere-FPGA/ipc")
```

---

### `shake_events.jsonl` — dropper shake start/stop log

Append-only log.  One JSON object per line, oldest first.  A new line is
written every time the Shake Dropper procedure starts or stops shaking.
The file is pruned to ≤ 3 MB by dropping the oldest lines; recent history
is always preserved.

Each line has this structure:

```json
{"ts_utc": 1745000100.123, "kind": "start", "amplitude_vpp": 0.10, "step": 0}
{"ts_utc": 1745000105.456, "kind": "stop",  "amplitude_vpp": 0.35, "step": 5}
```

| Field | Type | Notes |
|-------|------|-------|
| `ts_utc` | float | `time.time()` when the event occurred. |
| `kind` | `"start"` \| `"stop"` | Whether shaking began or ended. |
| `amplitude_vpp` | float | AWG amplitude at the moment of the event (Vpp). |
| `step` | int | Amplitude-ramp step index at the moment of the event. |

**How to use in the DAQ:**
At the end of each h5 recording, read all events whose `ts_utc` falls within
the recording window `[file_open_utc, file_close_utc]` and write them as
datasets inside the h5 file.  Timestamps stored in the h5 file should be
expressed as seconds relative to the file's own start time, so that the shake
events align naturally with the signal data.

```python
import json, pathlib

def read_shake_events(ipc_dir, t_start_utc, t_end_utc):
    """Return shake events in [t_start_utc, t_end_utc], oldest first."""
    path = pathlib.Path(ipc_dir) / "shake_events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if t_start_utc <= ev["ts_utc"] <= t_end_utc:
                events.append(ev)
        except (json.JSONDecodeError, KeyError):
            continue
    return events


# --- At h5 file close ---
import numpy as np

t_open  = ...   # time.time() when the h5 file was opened
t_close = ...   # time.time() when the h5 file is being closed

events = read_shake_events("/path/to/usphere-FPGA/ipc", t_open, t_close)

if events:
    # Store timestamps relative to the start of the recording
    ts_rel   = np.array([ev["ts_utc"]       - t_open for ev in events])
    amps     = np.array([ev["amplitude_vpp"]          for ev in events])
    steps    = np.array([ev["step"]                   for ev in events])
    kinds    = np.array([ev["kind"]                   for ev in events],
                        dtype=h5py.special_dtype(vlen=str))

    grp = h5file.require_group("shake_events")
    grp.create_dataset("timestamps_s",    data=ts_rel)
    grp.create_dataset("amplitude_vpp",   data=amps)
    grp.create_dataset("step",            data=steps)
    grp.create_dataset("kind",            data=kinds)
    grp.attrs["n_events"] = len(events)
```

**Typical shake cadence:** one start/stop pair every ~5 s during a trapping
attempt.  A 30 s h5 file typically contains ~6 pairs (~12 events).

---

## Locating the ipc/ directory

The FPGA program always writes to `<usphere-FPGA repo root>/ipc/`.  Pass this
path to the DAQ as a config value or environment variable; do not hard-code it.

```python
IPC_DIR = os.environ.get("USPHERE_FPGA_IPC", "/path/to/usphere-FPGA/ipc")
```

---

## What to do if the FPGA program is not running

- `tic_state.json` absent → store `null` / `NaN` for pressure in the h5 file and log a warning.
- `shake_events.jsonl` absent or empty window → store an empty dataset (zero rows); do not treat this as an error.
