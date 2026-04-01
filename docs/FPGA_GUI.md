# fpga_gui.py — GUI Architecture Reference

`fpga_gui.py` is the PyQt5 graphical interface for the FPGA control system.
It provides an 8-tab layout that mirrors the LabVIEW front panel
(`feedback(Host)_PIDXYZ_laserPID_NEW.vi`), with additional features for
real-time monitoring and frequency-domain analysis.

---

## Table of Contents

1. [Entry Point](#entry-point)
2. [Thread-Safe Signal Bridge](#thread-safe-signal-bridge)
3. [Helper Functions](#helper-functions)
4. [FPGAMainWindow](#fpgamainwindow)
   - [Instance Variables](#instance-variables)
   - [Tab Overview](#tab-overview)
5. [Tab Details](#tab-details)
   - [Connection Tab](#connection-tab)
   - [PID Feedback Tabs (X, Y, Z)](#pid-feedback-tabs-x-y-z)
   - [Waveform Tab](#waveform-tab)
   - [Outputs Tab](#outputs-tab)
   - [All Registers Tab](#all-registers-tab)
   - [Monitor Tab](#monitor-tab)
6. [Widget-Building Helpers](#widget-building-helpers)
7. [Session Persistence](#session-persistence)
8. [Connection Slots](#connection-slots)
9. [Register Operations](#register-operations)
10. [Change Pars](#change-pars)
11. [Boost](#boost)
12. [Ramp Operations](#ramp-operations)
13. [Arb Waveform](#arb-waveform)
14. [Sphere Save / Load](#sphere-save--load)
15. [Monitor Control](#monitor-control)
16. [Snapshots](#snapshots)
17. [Status Log](#status-log)

---

## Entry Point

```bash
python fpga_gui.py
```

The `main()` function creates a `QApplication` with the Fusion style,
instantiates `FPGAMainWindow`, and enters the Qt event loop.

```python
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = FPGAMainWindow()
    window.show()
    sys.exit(app.exec_())
```

---

## Thread-Safe Signal Bridge

The FPGA monitor runs in a background thread, but Qt requires UI updates
to happen on the main thread.  The `_Signals` class bridges this gap:

```python
class _Signals(QObject):
    status_message = pyqtSignal(str)        # log messages
    registers_updated = pyqtSignal(dict)    # poll results
    connected = pyqtSignal()                # connection established
    disconnected = pyqtSignal()             # connection closed
```

The controller's callbacks emit signals, and the GUI connects those signals
to its slot methods.

---

## Helper Functions

| Function | Description |
|----------|-------------|
| `_make_edit(value, readonly, width)` | Create a `QLineEdit` for a numeric value. Read-only fields get a grey background. |
| `_fmt(v)` | Format a float for display: uses scientific notation for very small/large values, otherwise 6 significant digits. |
| `_float(edit)` | Parse a `QLineEdit`'s text to `float` (returns `0.0` on parse failure). |

---

## FPGAMainWindow

The main window class (`QMainWindow`).  Window size: 1400 x 900 pixels.
Title: "usphere — FPGA Control".
Icon: `assets/Logo_transparent_outlined.PNG` (if file exists).

### Instance Variables

| Variable | Type | Description |
|----------|------|-------------|
| `_signals` | `_Signals` | Qt signal bridge for thread-safe UI updates |
| `_ctrl` | `FPGAController` | Backend controller (from `fpga_core.py`) |
| `_reg_edits` | `dict[str, QLineEdit]` | FPGA register → input widget mapping |
| `_host_edits` | `dict[str, QLineEdit]` | Host parameter → input widget mapping |
| `_host_values` | `dict[str, float]` | Current host parameter values |
| `_bead_fb_combos` | `dict[str, QComboBox]` | Per-axis bead feedback selector (Normal/Inverted) |
| `_boost_multiplier` | `float` | Boost gain factor (default: 10.0) |
| `_bitfile_edit` | `QLineEdit` | Bitfile path input |
| `_resource_edit` | `QLineEdit` | NI resource string input |
| `_poll_spin` | `QSpinBox` | Poll interval (50–5000 ms) |
| `_status_text` | `QTextEdit` | Status log display (Consolas 9pt, read-only) |
| `_plot_widget` | `FPGAPlotWidget` | Real-time plot widget (Monitor tab) |

### Tab Overview

| Index | Tab name | Build method |
|-------|----------|-------------|
| 0 | Connection | `_build_connection_tab()` |
| 1 | X Feedback | `_build_pid_tab("X")` |
| 2 | Y Feedback | `_build_pid_tab("Y")` |
| 3 | Z Feedback | `_build_pid_tab("Z")` |
| 4 | Waveform | `_build_waveform_tab()` |
| 5 | Outputs | `_build_outputs_tab()` |
| 6 | All Registers | `_build_registers_tab()` |
| 7 | Monitor | `_build_plot_tab()` |

---

## Tab Details

### Connection Tab

**Left panel (2/5 width):**

- **Connection** group:
  - Bitfile path with Browse button (`.lvbitx` filter)
  - Resource string (e.g. `PXI1Slot2`), fixed width 120px
  - Poll interval spinner (50–5000 ms, suffix " ms")
  - Connect / Disconnect buttons
  - Connection status label (red "Disconnected" / green "Connected" or "Connected (SIM)")

- **Actions** group:
  - "Read All Registers" button
  - "Start Monitor" / "Stop Monitor" buttons
  - "Save Snapshot" / "Load Snapshot" buttons (side by side)
  - "Save Sphere" / "Load Sphere" buttons (side by side)

- **Global** group:
  - Displays these registers inline: `Big Number`, `Count(uSec)`, `FPGA Error Out`,
    `Stop`, `X_emergency_threshould`, `Y_emergency_threshould`, `No_integral_gain`,
    `master x`, `master y`
  - Each has a "Set" button if writable

**Right panel (3/5 width):**

- **Status** group:
  - Read-only `QTextEdit` showing timestamped status messages (Consolas 9pt)

---

### PID Feedback Tabs (X, Y, Z)

Generated by `_build_pid_tab(axis)`.  Each tab is a scrollable vertical layout
containing these groups:

1. **Bead Feedback — {A} Axis**
   - Bead feedback dropdown (Normal / Inverted)
   - Registers: Setpoint, DC offset, pg, ig, dg, dg band, modulation gain,
     upper/lower limits
   - Indicators: AI plot, fb plot, tot_fb plot

2. **Before-Chamber PID** (labeled "AOM Feedback — Before Chamber" for Z)
   - Registers: Use PID before (bool), before Setpoint, pg before, ig before,
     dg before, dg band before, upper/lower limits before
   - Indicators: AI before chamber plot, fb before chamber plot

3. **Misc — {A}**
   - Registers: activate COM{a}, Reset {a} accum
   - Z only: accum reset z1, accum out z1, accurrm reset z2, accum out z2, pz?

4. **Filter Parameters (Hz)** — host-side inputs
   - hp freq, lp freq, LP FF, before-chamber HP/LP, bandpass HP/LP,
     before-chamber bandpass HP/LP
   - Z only: LP FF Z before

5. **Notch Filters** — host-side inputs
   - For each notch 1–4: frequency (Hz) and Q factor

6. **Computed Coefficients** — read-only FPGA registers showing the
   results of coefficient conversion
   - HP/LP coefficients, band coefficients, final filter, notch coefficients

7. **Ramp Power** (Z only)
   - Host params: End value power, Step power, Delay Time (s) power
   - "Ramp Power" button

8. **Action buttons** (bottom row)
   - **Change Pars** (bold, "Change Pars 2" for Z) — batch-write coefficients + PID values
   - **Reset {a} accum** — write `1.0` to reset register
   - **Save Sphere** — save all parameters
   - **Boost** (orange, bold) — multiply gains by boost factor

---

### Waveform Tab

1. **Bead Arbitrary Drive** group
   - File path input with Browse button (`.txt`, `.csv`, `.dat` filter)
   - "Write Data to Buffers" button

2. **Arb Gain** group
   - Registers: Arb gain (ch0), (ch1), (ch2), Arb steps per cycle,
     ready_to_write (READ), written_address (READ)

3. **Ramp Arb Drive** group
   - Host params: End value arb (ch0/ch1), Step arb (ch0/ch1),
     Delay Time (s) arb, z arb scale
   - "Ramp Arb Drive" button

---

### Outputs Tab

Scrollable layout with three groups:

1. **EOM** group
   - Registers: EOM_amplitude, EOM_threshold, EOM_offset, EOM_seed,
     Amplitude_sine_EOM, eom sine frequency (periods/tick),
     EOM_amplitude_out (READ), EOM reset (bool)
   - Host param: Frequency_sine_EOM (Hz)

2. **COM Output (Cluster)** group
   - Registers: Trigger for COM out (bool), offset, amplitude,
     frequency (periods/tick), duty cycle (periods)
   - Host param: frequency (kHz)

3. **AO Channels / Rotation Control** group
   - Rotation booleans: Reset voltage, If revert AO4 and AO5,
     If scan frequency (AO6 and AO7)?
   - For each channel (AO4, AO5, AO6, AO7):
     - Section header (e.g. "--- AO4 ---")
     - Registers: Amplitude, phase offset, frequency (FPGA units), reset (bool)
     - Host param: frequency (Hz)

---

### All Registers Tab

A scrollable list of all ~155 registers, organized by `Category`.
Each category is a collapsible `QGroupBox` (checkable, default checked).
Every register has a label, edit field, and "Set" button (if writable).

---

### Monitor Tab

Embeds an `FPGAPlotWidget` instance.  See [FPGA_PLOT.md](FPGA_PLOT.md)
for full documentation.

---

## Widget-Building Helpers

### `_add_reg(grid, row, name) → int`

Adds one FPGA register row to a `QGridLayout`:
- Column 0: `QLabel` with register name
- Column 1: `QLineEdit` (read-only if `Access.READ`, 50px for bools, 100px otherwise)
- Column 2: "Set" `QPushButton` (40px, only if writable)
- Registers are tracked in `_reg_edits` dict
- Returns `row + 1`

### `_add_host(grid, row, name) → int`

Adds one host-parameter row to a `QGridLayout`:
- Column 0: `QLabel` with parameter name
- Column 1: `QLineEdit` with default value and tooltip
- Tracked in `_host_edits` dict
- Returns `row + 1`

---

## Session Persistence

### `_restore_session()`

Called at construction.  Reads the last entry from `fpga_session_log.jsonl`
(via `load_last_session()`) and restores:
- Bitfile path
- Resource string
- Poll interval

### `_current_config() → FPGAConfig`

Reads current values from GUI widgets and returns an `FPGAConfig`.

### `_gather_host_params() → dict[str, float]`

Reads all host-parameter values from their `QLineEdit` widgets,
updates `_host_values`, and returns a copy.

---

## Connection Slots

| Slot | Trigger | Action |
|------|---------|--------|
| `_browse_bitfile()` | Browse button | File dialog for `.lvbitx` files |
| `_on_connect_clicked()` | Connect button | Build config, call `ctrl.connect()` |
| `_on_disconnect_clicked()` | Disconnect button | Call `ctrl.disconnect()` |
| `_on_connected()` | `connected` signal | Update label to green, disable bitfile/resource editing, read all registers |
| `_on_disconnected()` | `disconnected` signal | Update label to red, re-enable editing |

---

## Register Operations

| Method | Description |
|--------|-------------|
| `_write_one(name)` | Write register from its `_reg_edits` widget |
| `_write_one_edit(name, edit)` | Write register from a specific `QLineEdit` |
| `_write_one_value(name, value)` | Write a register with a specific float value |
| `_on_read_all()` | Read all registers and update all edit widgets |
| `_on_registers_updated(values)` | Called by monitor — updates edits + pushes to plot |
| `_update_reg_edits(values)` | Update all `QLineEdit` widgets from a values dict |

---

## Change Pars

`_on_change_pars(axis)` — triggered by the "Change Pars" button on each PID tab.

1. Gathers all host parameters via `_gather_host_params()`
2. Collects PID register values from the GUI edits:
   - `pg`, `ig`, `dg`, `dg band` (main + before)
   - Upper/lower limits (main + before)
   - Setpoint, DC offset, before Setpoint
   - `Use {A} PID before`
3. Handles the integral-gain naming quirk: `"ig X"` vs `" ig Y"` / `" ig Z"`
4. Calls `ctrl.change_pars(axis, host, pid_values)`
5. Reports errors and re-reads all registers

---

## Boost

`_on_boost(axis)` — triggered by the orange "Boost" button.

1. Reads current values of `pg {A}`, `dg {A}`, `dg band {A}` from GUI edits
2. Multiplies each by `_boost_multiplier` (default: 10.0)
3. Writes the boosted values via `ctrl.write_many()`
4. Reports success/failure and re-reads all registers

---

## Ramp Operations

| Method | Button | Action |
|--------|--------|--------|
| `_on_ramp_power()` | "Ramp Power" (Z tab) | Ramps `DC offset Z` from current to `End value power` with `Step power` and `Delay Time (s) power` |
| `_on_ramp_arb()` | "Ramp Arb Drive" (Waveform tab) | Ramps `Arb gain (ch0)` and `(ch1)` to their end values with per-channel steps |

Both validate that step > 0 before starting.

---

## Arb Waveform

| Method | Description |
|--------|-------------|
| `_browse_arb_file()` | File dialog for `.txt`, `.csv`, `.dat` files |
| `_on_write_arb_buffers()` | Calls `ctrl.load_arb_waveform(filepath)` |

---

## Sphere Save / Load

| Method | Description |
|--------|-------------|
| `_on_save_sphere()` | File dialog → `ctrl.save_sphere(path, host_params)`. Default filename: `sphere_YYYYMMDD_HHMMSS.json` |
| `_on_load_sphere()` | File dialog → `ctrl.load_sphere(path)`. Restores host params to GUI widgets, re-reads registers. |

---

## Monitor Control

| Method | Description |
|--------|-------------|
| `_on_start_monitor()` | Updates poll interval in config, calls `ctrl.start_monitor()` |
| `_on_stop_monitor()` | Calls `ctrl.stop_monitor()` |

---

## Snapshots

| Method | Description |
|--------|-------------|
| `_on_save_snapshot()` | File dialog → `ctrl.save_snapshot(path)`. Default: `fpga_snapshot_YYYYMMDD_HHMMSS.json` |
| `_on_load_snapshot()` | File dialog → `ctrl.load_snapshot(path)`. Reports errors, re-reads all. |

---

## Status Log

`_append_status(msg)` — prepends a `[HH:MM:SS]` timestamp and appends
the message to the status `QTextEdit`.

---

## Cleanup

`closeEvent(event)` — calls `ctrl.disconnect()` before the window closes
to ensure graceful shutdown of the FPGA session and monitor thread.
