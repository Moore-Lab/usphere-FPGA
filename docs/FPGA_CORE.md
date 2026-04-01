# fpga_core.py — FPGA Backend Reference

`fpga_core.py` is the backend module that manages a **persistent FPGA session**
with the NI PXIe-7856R.  It handles all hardware communication: opening /
closing the nifpga session, reading and writing registers, ramping values,
loading arbitrary waveforms, saving/loading snapshots and sphere parameters,
and running a polling monitor thread for live GUI updates.

The GUI (`fpga_gui.py`) imports this module, but it can also be used
standalone in scripts or Jupyter notebooks.

---

## Table of Contents

1. [Simulation Mode](#simulation-mode)
2. [Session Log](#session-log)
3. [FPGAConfig](#fpgaconfig)
4. [FPGAController](#fpgacontroller)
   - [Properties](#properties)
   - [Connection](#connection)
   - [Reading Registers](#reading-registers)
   - [Writing Registers](#writing-registers)
   - [Snapshots](#snapshots)
   - [Change Pars](#change-pars)
   - [Ramping](#ramping)
   - [Arbitrary Waveform Loading](#arbitrary-waveform-loading)
   - [Save / Load Sphere](#save--load-sphere)
   - [Monitor (Polling Thread)](#monitor-polling-thread)
5. [Callbacks](#callbacks)
6. [Thread Safety](#thread-safety)
7. [Internal Methods](#internal-methods)

---

## Simulation Mode

When the `nifpga` package is not installed, the module automatically enters
simulation mode:

- `FPGAController.is_simulated` returns `True`
- All reads return values from an in-memory dictionary (initialized to `0.0`)
- All writes update that in-memory dictionary
- The GUI runs identically — useful for development and testing without hardware

```python
from fpga_core import NIFPGA_AVAILABLE
print(NIFPGA_AVAILABLE)  # False if nifpga not installed
```

---

## Session Log

Every meaningful action is appended to `fpga_session_log.jsonl` (one JSON
object per line) for audit and session restoration.

### Module-level functions

| Function | Description |
|----------|-------------|
| `_append_log(action, data)` | Append a timestamped JSON entry to the log file |
| `load_last_session()` | Read and return the last log entry as a dict (or `None`) |

### Log entry format

```json
{
  "timestamp": "2026-03-31T14:23:01.123456",
  "action": "write",
  "register": "pg Z",
  "value": 1.5
}
```

### Actions logged

| Action | Triggered by | Extra fields |
|--------|-------------|-------------|
| `"connect"` | `ctrl.connect()` | `bitfile`, `resource`, `poll_interval_ms` |
| `"write"` | `ctrl.write_register()` | `register`, `value` |
| `"write_many"` | `ctrl.write_many()` | `values`, `errors` |
| `"change_pars"` | `ctrl.change_pars()` | `axis`, `host_params`, `coefficients`, `errors` |
| `"ramp_start"` | `ctrl.ramp_register()` | `register`, `target`, `step`, `delay_s` |
| `"arb_load"` | `ctrl.load_arb_waveform()` | `file`, `samples` |

---

## FPGAConfig

Dataclass holding connection and polling parameters.

```python
from fpga_core import FPGAConfig

cfg = FPGAConfig(
    bitfile  = r"C:\path\to\bitfile.lvbitx",
    resource = "PXI1Slot2",
    poll_interval_ms = 200,
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `bitfile` | `str` | *(lab default path)* | Absolute path to the compiled `.lvbitx` FPGA bitfile |
| `resource` | `str` | `"PXI1Slot2"` | NI resource string (visible in NI MAX) |
| `poll_interval_ms` | `int` | `200` | Monitor polling interval in milliseconds |

### Default bitfile

The default `bitfile` value points to:
```
C:\Users\yalem\GitHub\Documents\Optlev\LabView Code\FPGA code\FPGA Bitfiles\
Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx
```

### Serialization

```python
d = cfg.to_dict()                  # → {"bitfile": "...", "resource": "...", ...}
cfg2 = FPGAConfig.from_dict(d)     # reconstruct from dict (ignores unknown keys)
```

---

## FPGAController

The main class.  One instance manages a single persistent FPGA session.

```python
from fpga_core import FPGAController, FPGAConfig

ctrl = FPGAController(
    config=FPGAConfig(...),
    on_status=print,
    on_registers_updated=my_callback,
    on_connected=on_connect_handler,
    on_disconnected=on_disconnect_handler,
)
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `ctrl.is_connected` | `bool` | Whether the session is currently open |
| `ctrl.is_simulated` | `bool` | Whether nifpga is unavailable (simulation mode) |
| `ctrl.config` | `FPGAConfig` | Current configuration (mutable before connect) |

---

### Connection

```python
ctrl.connect()      # Open FPGA session (or enter simulation mode)
ctrl.disconnect()   # Close the session and stop the monitor
```

**`connect()` behavior:**
1. If already connected, returns immediately (no-op)
2. If `nifpga` is available: opens `nifpga.Session(bitfile, resource, run=False, reset_if_last_session_on_exit=False)`
3. If `nifpga` is not available: enters simulation mode, logs `[SIM]` message
4. Appends `"connect"` entry to session log
5. Invokes `on_connected()` callback

**`disconnect()` behavior:**
1. Calls `stop_monitor()` to stop the polling thread
2. Acquires `_lock`, closes the nifpga session (if real hardware)
3. Sets `is_connected` to `False`
4. Invokes `on_disconnected()` callback

---

### Reading Registers

```python
# Read a single register → float
value = ctrl.read_register("Z Setpoint")

# Read all ~155 registers → dict[str, float]
all_values = ctrl.read_all()
```

- Thread-safe (protected by `_lock`)
- In simulation mode: returns the in-memory value (default `0.0`)
- Boolean registers are converted: `True → 1.0`, `False → 0.0`
- Exceptions during hardware read return `0.0` (fail-safe)

---

### Writing Registers

```python
# Single register
ctrl.write_register("pg Z", 1.5)

# Multiple registers at once
errors = ctrl.write_many({
    "pg Z": 1.5,
    " ig Z": 0.3,
    "dg Z": 0.8,
    "Upper lim Z": 10.0,
})
# errors: dict[str, str] — empty dict if all succeeded
```

**`write_register(name, value)`**:
- Raises `KeyError` if register name is unknown
- Raises `ValueError` if register is read-only (`Access.READ`)
- Boolean registers: value is automatically converted via `bool(value)`
- Appends `"write"` entry to session log

**`write_many(values)`**:
- Writes multiple registers under one lock acquisition
- Returns `dict[str, str]` mapping failed register names to error messages
- Returns empty dict `{}` on complete success
- Skips unknown registers and read-only registers (records error, continues with rest)
- Appends `"write_many"` entry to session log

---

### Snapshots

Save and restore the complete register state as JSON:

```python
# Get snapshot dict without saving to file
snap = ctrl.snapshot()
# → {"timestamp": "...", "config": {...}, "simulated": false, "registers": {...}}

# Save snapshot to file (creates directories if needed)
path = ctrl.save_snapshot("data/my_setup.json")

# Load and restore registers from file
errors = ctrl.load_snapshot("data/my_setup.json")
```

**Snapshot JSON structure:**
```json
{
  "timestamp": "2026-03-31T14:23:01.123456",
  "config": {
    "bitfile": "C:\\path\\to\\bitfile.lvbitx",
    "resource": "PXI1Slot2",
    "poll_interval_ms": 200
  },
  "simulated": false,
  "registers": {
    "Z Setpoint": 0.0,
    "pg Z": 1.5,
    " ig Z": 0.3,
    ...
  }
}
```

**`load_snapshot(filepath)`**:
- Reads JSON and writes only writable registers (read-only registers are skipped)
- Returns `dict[str, str]` of write errors

---

### Change Pars

Batch-compute filter coefficients from host-side frequency/Q parameters
and write them (along with PID gains) to the FPGA in a single operation:

```python
errors = ctrl.change_pars(
    axis="Z",
    host_params={
        "hp freq Z": 4000.0,
        "lp freq Z": 4000.0,
        "LP FF Z": 4200.0,
        "notch freq 1 z": 960.0,
        "notch Q 1 z": 4.0,
        # ...
    },
    pid_values={          # optional — raw PID register values
        "pg Z": 1.5,
        " ig Z": 0.3,
        "dg Z": 0.8,
    },
)
```

**Behavior:**
1. Calls `compute_coefficients(axis, host_params)` from `fpga_registers.py`
2. Merges computed coefficients with `pid_values` (PID values take precedence on overlap)
3. Calls `write_many()` on the combined dict
4. Logs `"change_pars"` with all details (axis, host_params, coefficients, errors)
5. Returns error dict

---

### Ramping

Smoothly transition a register from its current value to a target:

```python
thread = ctrl.ramp_register(
    name="DC offset Z",
    target=3000.0,
    step=100.0,       # absolute step size (sign is auto-determined)
    delay_s=0.05,     # seconds between steps
    callback=None,    # optional callable(current_value) after each step
)
# Returns a started daemon Thread
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Register to ramp |
| `target` | `float` | Final value to reach |
| `step` | `float` | Step size per iteration (must be > 0; direction is automatic) |
| `delay_s` | `float` | Delay between steps in seconds |
| `callback` | `callable\|None` | Optional: called with current value after each step |

**Behavior:**
1. Reads the current register value
2. Determines ramp direction automatically (up or down)
3. Increments by `step` each iteration, sleeping `delay_s` between
4. Writes exact `target` on the final step (no overshoot)
5. Stops early if the controller disconnects mid-ramp
6. Logs `"ramp_start"` at initiation

---

### Arbitrary Waveform Loading

Load waveform data from a text file into the FPGA's 3 data buffers:

```python
ctrl.load_arb_waveform("data/my_waveform.txt")
```

**Expected file format:**
- One sample per line
- Up to 3 columns (whitespace- or comma-separated)
- Column 1 → `data_buffer_1`, Column 2 → `data_buffer2`, Column 3 → `data_buffer3`

```
0.000   0.100   0.000
0.010   0.095   0.005
0.020   0.090   0.010
```

**Handshake protocol:**
1. For each sample index `i`:
   - Poll `ready_to_write` until truthy (up to 1000 attempts, 1 ms apart)
   - Write `write_address = i`
   - Write column values to buffer registers
2. Logs `"arb_load"` with filename and sample count

---

### Save / Load Sphere

Save and restore per-sphere parameters (registers + host params):

```python
# Save registers and host params together
path = ctrl.save_sphere("data/sphere_5um.json", host_params=gathered_dict)

# Load — returns (write_errors, loaded_host_params)
errors, host = ctrl.load_sphere("data/sphere_5um.json")
```

**Sphere file structure** (extends snapshot format):
```json
{
  "timestamp": "...",
  "config": {...},
  "simulated": false,
  "registers": {...},
  "host_params": {
    "hp freq Z": 4000.0,
    "notch freq 1 z": 960.0,
    ...
  }
}
```

**`save_sphere(filepath, host_params=None)`**:
- Creates a snapshot and adds `"host_params"` key if provided
- Creates parent directories if needed

**`load_sphere(filepath)`**:
- Writes all writable registers from the file
- Returns `(errors_dict, host_params_dict)` — GUI uses host_params to restore fields

---

### Monitor (Polling Thread)

Background daemon thread that periodically reads all registers:

```python
ctrl.start_monitor()   # Start polling at config.poll_interval_ms interval
ctrl.stop_monitor()    # Stop polling (blocks up to 2 seconds for thread join)
```

**Behavior:**
- No-op if already running
- Polls at `config.poll_interval_ms` interval (converted to seconds internally)
- Each cycle: `read_all()` → passes result to `on_registers_updated(values)`
- Thread is a daemon — automatically dies when main process exits
- `stop_monitor()` sets a `threading.Event` and joins the thread

---

## Callbacks

Four optional callbacks can be provided at construction:

| Callback | Signature | When called |
|----------|-----------|-------------|
| `on_status` | `(msg: str) → None` | Log / status messages; defaults to `print` |
| `on_registers_updated` | `(values: dict[str, float]) → None` | Each monitor poll cycle with all register values |
| `on_connected` | `() → None` | After successful `connect()` |
| `on_disconnected` | `() → None` | After `disconnect()` |

The GUI bridges these through a `_Signals` QObject that emits pyqtSignals,
ensuring thread-safe UI updates from the monitor thread.

---

## Thread Safety

| Mechanism | Purpose |
|-----------|---------|
| `_lock` (`threading.Lock`) | Protects all register read/write operations |
| `_monitor_stop` (`threading.Event`) | Coordinates monitor shutdown |
| `_monitor_thread` (`threading.Thread`, daemon) | Background polling |
| Ramp threads (daemon) | Each `ramp_register()` spawns a daemon thread |

All public methods are safe to call from any thread.

---

## Internal Methods

| Method | Description |
|--------|-------------|
| `_log(msg)` | Forward a message to the `on_status` callback |
| `_read_one(name)` | Read one register; caller must hold `_lock` |
| `_write_one(name, value, reg)` | Write one register; caller must hold `_lock` |
| `_monitor_loop()` | The polling thread's main loop body |
