# User Guide

Step-by-step guide for operating the **usphere-FPGA** GUI — the Python control
interface for the NI PXIe-7856R microsphere feedback system.

---

## Table of Contents

1.  [First Launch](#1-first-launch)
2.  [Connecting to the FPGA](#2-connecting-to-the-fpga)
3.  [Connection Tab Overview](#3-connection-tab-overview)
4.  [Feedback Tabs (X / Y / Z)](#4-feedback-tabs-x--y--z)
5.  [Tuning PID Gains](#5-tuning-pid-gains)
6.  [Using Change Pars](#6-using-change-pars)
7.  [Setting Filters (Low-Pass, High-Pass, Notch)](#7-setting-filters-low-pass-high-pass-notch)
8.  [Using Boost](#8-using-boost)
9.  [Ramping Power (Z Axis)](#9-ramping-power-z-axis)
10. [Waveform Tab — Arb Waveform Loading](#10-waveform-tab--arb-waveform-loading)
11. [Outputs Tab — EOM, COM, and Rotation](#11-outputs-tab--eom-com-and-rotation)
12. [All Registers Tab](#12-all-registers-tab)
13. [Monitor Tab — Live Plots](#13-monitor-tab--live-plots)
14. [Reading the PSD Plot](#14-reading-the-psd-plot)
15. [Saving and Loading Snapshots](#15-saving-and-loading-snapshots)
16. [Saving and Loading Spheres](#16-saving-and-loading-spheres)
17. [Session Log](#17-session-log)
18. [Keyboard Shortcuts and Tips](#18-keyboard-shortcuts-and-tips)
19. [Common Workflows](#19-common-workflows)
20. [FAQ](#20-faq)

---

## 1. First Launch

```bash
# Activate the virtual environment
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS

# Launch the GUI
python fpga_gui.py
```

The window opens with **8 tabs** across the top. On first launch the GUI is in
**disconnected** state — most controls are present but writes will fail until
you connect to the FPGA (or run in simulation mode).

If `nifpga` is not installed, the GUI automatically starts in **simulation
mode** and prints a banner in the status log at the bottom of the Connection
tab. All features work against an in-memory register store.

---

## 2. Connecting to the FPGA

1. Navigate to the **Connection** tab (first tab).
2. Set the **Bitfile** field to the full path of the `.lvbitx` file:
   ```
   C:\NI\Bitfiles\Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx
   ```
3. Set the **Resource** field to the NI MAX resource string (e.g.
   `PXI1Slot2`).
4. Click **Connect**.

On success the status log shows `Connected to PXI1Slot2` and the GUI
performs an initial **Read All** to populate every register field. On failure
an error message appears in the log.

To disconnect, click **Disconnect**. The FPGA session is closed gracefully.

---

## 3. Connection Tab Overview

The Connection tab contains:

| Section | Contents |
|---------|----------|
| **Connection controls** | Bitfile path, Resource string, Poll Interval (ms), Connect / Disconnect buttons |
| **Global registers** | Emergency X/Y thresholds, integral-gain disable booleans, master enables — these registers affect all axes |
| **Status log** | Scrolling text area showing connect/disconnect events, errors, and write confirmations |

### Poll Interval

The **Poll Interval (ms)** spin box sets how often the background monitor
thread reads all registers (default: 200 ms). Lower values give faster
updates but increase PXIe bus traffic.

---

## 4. Feedback Tabs (X / Y / Z)

Each axis feedback tab has the same layout:

```
┌─────────────────────────────────────────────┐
│  PID Gains          │  Filter Parameters     │
│  ─────────          │  ──────────────────     │
│  Proportional       │  LP freq (Hz)           │
│  Integral           │  HP freq (Hz)           │
│  Derivative         │  Notch 1 freq / Q       │
│  DG Band            │  Notch 2 freq / Q       │
│  Setpoint           │  Notch 3 freq / Q       │
│                     │  (Y/Z have Notch 4)     │
│─────────────────────│─────────────────────────│
│  Bead Feedback: [Normal ▾]                    │
│  [Change Pars]  [Boost]  Boost Factor: [10.0] │
│─────────────────────│─────────────────────────│
│  Computed Coefficients (read-only display)     │
│  LP coeff, HP coeff, Notch a1/a2/b0/b1/b2    │
└─────────────────────────────────────────────┘
```

- **Left column (Host Parameters):** Human-friendly values — frequencies in
  Hz, Q factors as dimensionless numbers, gains as floats.
- **Right column (FPGA Registers):** The raw register values that are
  actually written to hardware. Typically populated by **Change Pars** or by
  clicking **Set** on individual fields.

---

## 5. Tuning PID Gains

1. Navigate to the relevant axis tab (e.g. **X Feedback**).
2. Enter values for:
   - **pg** (proportional gain)
   - **ig** (integral gain)
   - **dg** (derivative gain)
   - **dg band** (derivative gain band-pass)
3. Click **Change Pars** to write all gains and computed coefficients to the
   FPGA in one batch.

**Or** use individual **Set** buttons to write one register at a time.

### Tips

- Start with low gains and increase gradually. Watch the PSD plot to see the
  effect on the noise spectrum.
- Proportional gain provides the fastest response but can cause oscillation
  if set too high.
- Integral gain eliminates steady-state error but reduces bandwidth.
- Derivative gain damps oscillations but amplifies high-frequency noise.
- Use the **dg band** parameter with the **band-pass** (LP + HP) filter
  range to limit the derivative to a useful frequency window.

---

## 6. Using Change Pars

**Change Pars** is the primary way to update all PID and filter parameters
for an axis in one atomic operation.

### What it does

1. Reads all host parameter fields on the current axis tab (pg, ig, dg, dg
   band, LP freq, HP freq, notch freqs/Q values, setpoint, etc.).
2. Calls `compute_coefficients(axis, host_params)` to convert frequencies
   and Q factors into FPGA filter coefficients (IIR low-pass, high-pass,
   and notch filter a/b terms).
3. Writes **all** gains and computed coefficients to the FPGA in a single
   `write_many()` call.
4. Logs the operation to the session log.

### When to use it

- After adjusting any combination of gains and/or filter parameters.
- When you want to ensure all related coefficients are updated atomically
  (avoids partial updates where, e.g., the LP filter is updated but the
  HP filter still has old coefficients).

### Coefficient Conversion Formulas

The low-pass and high-pass coefficients are exponential decay:

$$\alpha_{\text{LP}} = e^{-2\pi f / f_s}$$

$$\alpha_{\text{HP}} = e^{-2\pi f / f_s}$$

where $f_s = 100{,}000\;\text{Hz}$ is the FPGA sample rate derived from
`Count(uSec) = 10`.  See [FPGA_REGISTERS.md](FPGA_REGISTERS.md) for the
full set of notch filter equations.

---

## 7. Setting Filters (Low-Pass, High-Pass, Notch)

### Low-Pass / High-Pass

Enter the **cutoff frequency in Hz** in the host parameter field and click
**Change Pars**. The coefficient conversion happens automatically.

| Parameter | Host Field | Converted Register |
|-----------|------------|-------------------|
| LP cutoff | `lp freq X` | `lp coeff X` |
| HP cutoff | `hp freq X` | `hp coeff X` |

### Notch Filters

Each axis has 3–4 notch filters. Each notch has two host parameters:

- **Notch N freq (Hz)**: Centre frequency of the notch
- **Notch N Q**: Quality factor (higher Q = narrower notch)

When you click **Change Pars**, each notch is converted to five IIR
biquad coefficients:

| Coefficient | Meaning |
|-------------|---------|
| `a1` | Feedback coefficient 1 |
| `a2` | Feedback coefficient 2 |
| `b0` | Feedforward coefficient 0 |
| `b1` | Feedforward coefficient 1 |
| `b2` | Feedforward coefficient 2 |

### How to Place a Notch

1. Look at the **PSD plot** (Monitor tab) to identify a noise peak.
2. Read the frequency of the peak from the x-axis.
3. Go to the axis feedback tab and enter that frequency in a free Notch
   slot.
4. Set Q to control width — start with Q ≈ 5 for a moderate notch, increase
   for a narrower hole.
5. Click **Change Pars** to apply.
6. Check the PSD plot to confirm the peak is suppressed.

---

## 8. Using Boost

**Boost** temporarily multiplies the proportional gain (pg), derivative gain
(dg), and derivative gain band (dg band) by a configurable factor.

### How to use it

1. Set the **Boost Factor** spin box (default: 10.0).
2. Click **Boost**.
3. The button text changes to **Un-Boost** and the gains are multiplied.
4. Click **Un-Boost** to restore the original values.

### When to use it

- **Catching a falling sphere**: If the sphere is about to drop out of the
  trap, Boost provides a quick burst of higher feedback gain to re-centre it.
- **Quick recovery**: After a transient disturbance disrupts the feedback
  loop.

> **Caution:** Boosted gains may cause oscillation or clipping. Monitor the
> signals carefully and un-boost as soon as the sphere is re-stabilised.

---

## 9. Ramping Power (Z Axis)

The **Ramp Power** function on the Z Feedback tab smoothly ramps
`DC offset Z` from its current value to a target.

1. Enter the **target value** in the Ramp Target field.
2. Enter the **step size** and **delay (s)** between steps.
3. Click **Ramp Power**.

The ramp executes in a background thread so the GUI remains responsive.
Progress is logged to the status area.

### Why Ramp?

Abrupt changes to DC offset Z can cause a sphere to drop. Ramping ensures
slow, controlled transitions that the feedback loop can track.

---

## 10. Waveform Tab — Arb Waveform Loading

The Waveform tab allows you to load arbitrary waveform data from a text file
into the FPGA's three data buffers.

### Loading Steps

1. Enter the path to a waveform text file (one sample per line, or
   tab-separated columns for multi-channel).
2. Click **Write Arb Buffers**.
3. The data is loaded into the FPGA's three arb waveform DMA buffers.

### Ramping Arb Gains

The arb waveform channels have per-channel gains. Use **Ramp Arb Drive** to
smoothly change these gains:

1. Enter the target gains for each channel.
2. Enter step size and delay.
3. Click **Ramp Arb Drive**.

### Associated Registers

| Register | Description |
|----------|-------------|
| `Arb Waveform Gain 1/2/3` | Amplitude multipliers for each channel |
| `Steps per cycle` | Number of samples per waveform period |
| `address offset 1/2/3` | Starting offsets in the DMA buffers |
| `Arb Waveform On?` | Boolean enable for the arb waveform output |

---

## 11. Outputs Tab — EOM, COM, and Rotation

### EOM Controls

The electro-optic modulator (EOM) section provides:

| Control | Register | Description |
|---------|----------|-------------|
| Amplitude | `EOM_amplitude` | Drive amplitude |
| Threshold | `EOM_threshold` | Trigger threshold |
| Seed | `EOM_seed` | PRNG seed for noise modulation |
| Offset | `EOM_offset` | DC offset |
| Sine Frequency | `EOM_sine_frequency` | Sine modulation frequency |
| Amplitude Out | `EOM_amplitude_out` | Current amplitude indicator (read-only) |

### COM Output Cluster

| Control | Register | Description |
|---------|----------|-------------|
| Trigger | `COM_trigger` | External trigger enable |
| Offset | `COM_offset` | DC offset |
| Amplitude | `COM_amplitude` | Drive amplitude |
| Frequency | `COM_frequency` | Modulation frequency |
| Duty Cycle | `COM_duty_cycle` | PWM duty cycle |

### Rotation Controls (AO4–AO7)

Four analog output channels control rotation electrodes:

| Channel | Registers |
|---------|-----------|
| AO4 | `AO4 frequency`, `AO4 amplitude`, `AO4 phase`, `AO4 reset` |
| AO5 | `AO5 frequency`, `AO5 amplitude`, `AO5 phase`, `AO5 reset` |
| AO6 | `AO6 frequency`, `AO6 amplitude`, `AO6 phase`, `AO6 reset` |
| AO7 | `AO7 frequency`, `AO7 amplitude`, `AO7 phase`, `AO7 reset` |

Additional boolean controls:

| Control | Register | Purpose |
|---------|----------|---------|
| AO4 revert | `AO4 revert?` | Revert AO4 to default |
| AO5 revert | `AO5 revert?` | Revert AO5 to default |
| AO6 scan freq | `AO6 scan frequency?` | Enable frequency scan on AO6 |
| AO7 scan freq | `AO7 scan frequency?` | Enable frequency scan on AO7 |
| Reset Voltage | `Reset Voltage` | Master reset voltage for rotation |

---

## 12. All Registers Tab

The **All Registers** tab shows every one of the ~155 FPGA registers in a
scrollable list, grouped by category.

### Features

- **Search / filter**: Quickly find a register by name.
- **Read All**: Reads every register from the FPGA and updates all fields.
- **Write Changed**: Writes only registers whose fields have been modified
  since the last read.
- **Individual Set buttons**: Write a single register value.
- **Read-only indicators**: Greyed-out fields for registers with `Access.R`
  (status indicators, computed outputs).

### When to use it

- To inspect or modify a register that is not exposed on the axis tabs.
- To verify that a **Change Pars** operation wrote the expected coefficient
  values.
- For diagnostic purposes — checking raw register values against LabVIEW.

### Category Groups

Registers are grouped into sections with headers:

| Group | Examples |
|-------|---------|
| Status / Timing | Stop, FPGA Error Out, Count(uSec) |
| Z Axis | Z Setpoint, pg Z, ig Z, lp coeff Z, notch Z a1 1, ... |
| Y Axis | Y Setpoint, pg Y, ig Y, ... |
| X Axis | X Setpoint, pg X, ig X, ... |
| Arb Waveform | Arb Waveform Gain 1, Steps per cycle, ... |
| EOM | EOM_amplitude, EOM_threshold, ... |
| COM Output | COM_trigger, COM_offset, ... |
| Global | X_emergency_threshould, disable integral gain? Z, ... |
| AO Channels | AO4 frequency, AO5 amplitude, ... |

---

## 13. Monitor Tab — Live Plots

The Monitor tab hosts the **FPGAPlotWidget**, which provides two sub-tabs:

### Bead (Time Domain)

- Scrolling strip chart showing the most recent 2048 samples per channel.
- X-axis: time in seconds.
- Y-axis: raw register values (counts or volts depending on the register).
- Channels update every poll cycle (controlled by Poll Interval on the
  Connection tab).

### Plot 0 (PSD / FFT)

- Log-log power spectral density computed from the ring buffer.
- X-axis: frequency in Hz (log scale).
- Y-axis: power spectral density in $\text{V}^2/\text{Hz}$ (log scale).
- Use this to identify resonance peaks, noise floors, and the effect of
  notch/LP/HP filters.

### Enabling Channels

Below the plot is a row of **channel checkboxes**. Enable or disable channels
to control which signals are plotted. The 17 available channels are:

| # | Channel | Description |
|---|---------|-------------|
| 0 | X error | X-axis error signal |
| 1 | Y error | Y-axis error signal |
| 2 | Z error | Z-axis error signal |
| 3 | X out | X-axis control output |
| 4 | Y out | Y-axis control output |
| 5 | Z out | Z-axis control output |
| 6 | X monitor | X auxiliary monitor |
| 7 | Y monitor | Y auxiliary monitor |
| 8 | Z monitor | Z auxiliary monitor |
| 9 | Sum | Photodetector sum signal |
| 10 | EOM out | EOM drive output |
| 11 | COM out | COM drive output |
| 12 | AO4 out | AO4 channel output |
| 13 | AO5 out | AO5 channel output |
| 14 | AO6 out | AO6 channel output |
| 15 | AO7 out | AO7 channel output |
| 16 | Arb out | Arbitrary waveform output |

### Selecting a Window Function

A dropdown above the PSD plot lets you choose the FFT window:

| Window | Characteristics |
|--------|-----------------|
| **Hanning** (default) | Good general purpose; low spectral leakage |
| **Hamming** | Slightly better sidelobe suppression than Hanning |
| **Blackman** | Best sidelobe suppression; wider main lobe |
| **Boxcar** (rectangular) | No windowing; maximum resolution, highest leakage |

For identifying narrow spectral peaks (e.g. mechanical resonances), use
**Hanning** or **Blackman**. For broadband noise characterisation, any
window works.

---

## 14. Reading the PSD Plot

The PSD (Power Spectral Density) plot is the primary diagnostic for
feedback tuning.

### What to look for

| Feature | Indicates |
|---------|-----------|
| Narrow peak | Mechanical resonance or external vibration |
| Broad hump | Under-damped feedback or structural resonance |
| Rising slope at low freq | Insufficient high-pass filtering or drift |
| Flat floor | Detector noise floor |
| Notch (dip) | Active notch filter is suppressing a mode |

### Workflow for Feedback Tuning

1. **Start Monitor** on the Connection tab.
2. Switch to the **Monitor** tab and enable the channel of interest
   (e.g. Z error).
3. Select the **Plot 0** sub-tab to view the PSD.
4. Identify peaks in the spectrum.
5. Go to the axis feedback tab and either:
   - Increase gains to push the peak below the noise floor.
   - Place a notch filter at the peak frequency to suppress it.
6. Click **Change Pars** and watch the PSD update in real time.
7. Iterate until the spectrum is clean and the feedback is stable.

---

## 15. Saving and Loading Snapshots

A **snapshot** captures the current value of every register (~155 values) in
a JSON file.

### Save a Snapshot

1. Go to the **Connection** tab (or use the menu if available).
2. Click **Save Snapshot**.
3. A file dialog lets you choose the save location (default: `data/`
   directory).
4. The JSON file contains a timestamped dump of all register values.

### Load a Snapshot

1. Click **Load Snapshot**.
2. Select a previously saved JSON file.
3. All writable register values are restored to the FPGA.
4. The GUI fields update to reflect the loaded values.

### Use Cases

- Back up a known-good configuration before tuning.
- Share a configuration with another operator.
- Compare register values between different experimental runs.

---

## 16. Saving and Loading Spheres

**Sphere save** is more targeted than a full snapshot — it saves only the
PID gains, filter parameters, and host parameter values for a specific
sphere. This is the equivalent of the LabVIEW "Save Sphere" / "Load Sphere"
functionality.

### Save a Sphere

1. Click **Save Sphere** on the Connection tab.
2. Choose a file name (e.g. `sphere_5um_200mbar.json`).
3. The file stores:
   - All axis PID gains (pg, ig, dg, dg band)
   - Filter frequencies and Q values
   - Setpoints
   - Host parameter values (the human-readable Hz/Q values)

### Load a Sphere

1. Click **Load Sphere**.
2. Select a sphere JSON file.
3. The GUI populates all host parameter fields with the saved values.
4. Click **Change Pars** on each axis to write the parameters to the FPGA.

> **Note:** Load Sphere restores the *host parameter values* but does not
> automatically write them to the FPGA. You must click **Change Pars** to
> commit the values. This lets you review and modify them before applying.

---

## 17. Session Log

Every significant operation is logged to `fpga_session_log.jsonl` (one JSON
object per line):

```json
{"timestamp": "2025-01-15T14:32:01.123", "event": "connect", "resource": "PXI1Slot2"}
{"timestamp": "2025-01-15T14:32:05.456", "event": "write", "register": "pg Z", "value": 1.5}
{"timestamp": "2025-01-15T14:33:12.789", "event": "change_pars", "axis": "Z", "params": {...}}
```

This provides an audit trail for:

- Reproducing an experiment's control parameters.
- Debugging issues (what was the last thing written before the sphere dropped?).
- Reconstructing the timeline of parameter changes.

The log can be loaded back with `FPGAController.load_last_session()` for
programmatic analysis.

---

## 18. Keyboard Shortcuts and Tips

| Action | Shortcut / Tip |
|--------|---------------|
| **Navigate tabs** | Click tab headers or use `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| **Enter a value and write** | Type in a field, press `Enter`, then click **Set** |
| **Quick Read All** | On the All Registers tab, click **Read All** |
| **Focus a field** | Click on any editable field to start typing |
| **Resize plots** | Drag the splitter between the plot and channel checkboxes |

### General Tips

- **Always Read All after connecting** — the GUI does this automatically,
  but you can repeat it to confirm the FPGA state.
- **Use Change Pars, not individual Set** — Change Pars writes all related
  parameters atomically, avoiding transient intermediate states.
- **Save before tuning** — Save a snapshot or sphere before making changes.
  If things go wrong, Load Sphere to restore.
- **Keep Poll Interval ≥ 100 ms** — Values below 100 ms may saturate the
  PXIe bus and cause read errors.
- **Watch the status log** — It shows every write and any error messages.

---

## 19. Common Workflows

### Workflow A: Trapping a New Sphere

1. **Connect** to the FPGA.
2. **Load a sphere file** from a previous trapping session with similar
   parameters.
3. Review gains on each axis tab. Adjust if needed.
4. Click **Change Pars** on X, Y, and Z.
5. **Start Monitor** and watch the error signals.
6. **Ramp Power** slowly using the Z Feedback tab.
7. Once trapped, fine-tune PID gains while watching the PSD.
8. **Save Sphere** once the trap is stable.

### Workflow B: Identifying and Suppressing a Noise Peak

1. **Start Monitor** and switch to the **Plot 0 (PSD)** sub-tab.
2. Enable the relevant error channel (e.g. Z error).
3. Identify the frequency of the noise peak on the x-axis.
4. Go to the **Z Feedback** tab.
5. Enter the peak frequency in an unused **Notch** slot.
6. Set **Q** — start with 5, increase for a sharper notch.
7. Click **Change Pars**.
8. Return to the PSD plot and verify the peak is suppressed.
9. Repeat for additional peaks if needed.

### Workflow C: Comparing Configurations

1. Load configuration A: **Load Snapshot** → file A.
2. Note key register values (or export from the session log).
3. Load configuration B: **Load Snapshot** → file B.
4. Compare the register values to identify differences.

### Workflow D: Emergency Sphere Recovery

1. If the sphere starts oscillating: click **Boost** on the relevant axis.
2. The gains increase by the Boost Factor (default 10x).
3. Once the sphere re-stabilises, click **Un-Boost** to return to normal
   gains.
4. Investigate the cause of the instability (check PSD for new noise
   sources).

---

## 20. FAQ

### Q: Can I run the GUI without the FPGA hardware?

**Yes.** If `nifpga` is not installed, the GUI starts in simulation mode.
All features work against an in-memory register store. Install PyQt5, numpy,
and matplotlib — no NI drivers needed.

### Q: Why do some register names have typos?

The register names (e.g. `X_emergency_threshould`, `accurrm reset z2`) come
directly from the LabVIEW bitfile and must match exactly for `nifpga` to
find them. The Python code uses these names as-is. Do not "fix" the spelling
or the FPGA communication will break.

### Q: What does "Count(uSec) = 10" mean?

This register sets the FPGA loop timing. A value of 10 means each loop
iteration takes 10 µs, so the sample rate is:

$$f_s = \frac{1}{10 \times 10^{-6}} = 100{,}000\;\text{Hz}$$

All filter coefficient calculations use this sample rate.

### Q: How do I know if a notch filter is active?

Check the **Computed Coefficients** section on the axis feedback tab. If a
notch's `b0/b1/b2/a1/a2` values are non-zero, the filter is active. On the
PSD plot, an active notch appears as a dip at the specified frequency.

### Q: What is the difference between a Snapshot and a Sphere save?

| | Snapshot | Sphere |
|---|---------|--------|
| **Scope** | All ~155 registers | PID gains + host params only |
| **Includes host params** | No (raw register values only) | Yes (Hz, Q, etc.) |
| **Use case** | Full state backup | Quick save/restore of tuning |
| **Load behaviour** | Writes all registers immediately | Populates GUI fields (needs Change Pars) |

### Q: How fast does the monitor update?

The monitor polls at the rate set by **Poll Interval** on the Connection tab
(default: 200 ms = 5 Hz). The PSD plot recalculates each time new data
arrives. With 2048 samples in the buffer at 5 Hz polling, the PSD has a
frequency resolution of about $\frac{5}{2048} \approx 0.0024\;\text{Hz}$.

However, the actual frequency axis extends to $f_s/2 = 50{,}000\;\text{Hz}$
because the data itself is sampled at 100 kHz by the FPGA. The PSD is
computed from whatever data is in the ring buffer.

### Q: Can I script the FPGA without the GUI?

**Yes.** Import `fpga_core` and `fpga_registers` directly:

```python
from fpga_core import FPGAController, FPGAConfig
from fpga_registers import compute_coefficients

ctrl = FPGAController(FPGAConfig(
    bitfile=r"C:\path\to\bitfile.lvbitx",
    resource="PXI1Slot2",
))
ctrl.connect()
ctrl.write_register("pg Z", 1.5)
coeffs = compute_coefficients("Z", {"hp freq Z": 4000.0})
ctrl.write_many(coeffs)
ctrl.disconnect()
```

See the [scripting example in the README](../README.md) for more.

### Q: The GUI is unresponsive during a ramp.

Ramps run in a background thread and should not block the GUI. If the GUI
feels slow, check the terminal for Python error tracebacks and ensure the
Poll Interval is not set too low (< 100 ms).

---

*See also: [SETUP.md](SETUP.md) · [FPGA_CORE.md](FPGA_CORE.md) · [FPGA_REGISTERS.md](FPGA_REGISTERS.md) · [FPGA_GUI.md](FPGA_GUI.md) · [FPGA_PLOT.md](FPGA_PLOT.md)*
