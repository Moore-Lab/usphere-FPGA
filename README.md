# usphere-FPGA

Python control interface for the **NI PXIe-7856R** FPGA module used in the
optically levitated microsphere experiment at Yale.  Replaces the LabVIEW
front panel (`feedback(Host)_PIDXYZ_laserPID_NEW.vi`) with a PyQt5 GUI that
reads and writes all ~155 FPGA registers interactively and converts host-side
frequency / Q parameters to FPGA filter coefficients in real time.

> **LabVIEW bitfile:**
> `Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx`

---

## Quick Start

```bash
# 1. Install (creates .venv and installs all dependencies)
python install_deps.py

# 2. Activate the virtual environment
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

# 3. Launch the GUI
python fpga_gui.py
```

The GUI runs in **simulation mode** when the `nifpga` driver is not
available, so you can develop and test the interface without hardware.

---

## Project Layout

```
usphere-FPGA/
├── fpga_registers.py     # Register definitions, host params, coefficient conversion
├── fpga_core.py          # FPGA session controller (connect, read, write, monitor)
├── fpga_gui.py           # PyQt5 GUI — 8 tabs matching the LabVIEW front panel
├── fpga_plot.py          # Real-time time-domain and PSD/FFT plotting widget
├── install_deps.py       # Virtual environment + dependency installer
├── requirements.txt      # Python package dependencies
├── README.md             # This file
├── assets/               # Icons and images (Logo_transparent_outlined.PNG)
├── data/                 # Snapshots, sphere saves, and output files
├── development/          # Hardware, measurement, and project notes
├── papers/               # Reference papers (PDFs)
├── resources/            # LabVIEW VI docs, usphere-DAQ sister project
│   ├── fpga_feedback/    # Exported LabVIEW front panel and block diagrams
│   └── usphere-DAQ/      # DAQ companion project
└── docs/                 # Detailed documentation
    ├── SETUP.md          # Installation and environment setup guide
    ├── USER_GUIDE.md     # Step-by-step user guide for all GUI operations
    ├── FPGA_CORE.md      # fpga_core.py API reference
    ├── FPGA_REGISTERS.md # Register table, host params, coefficient formulas
    ├── FPGA_GUI.md       # fpga_gui.py architecture and tab reference
    └── FPGA_PLOT.md      # fpga_plot.py plotting widget reference
```

---

## Architecture

Mirrors `usphere-DAQ` with symmetric module roles:

| Module | DAQ Equivalent | Role |
|--------|---------------|------|
| `fpga_registers.py` | `daq_fpga.CONTROL_NAMES` | Register names, categories, access modes, host params, coefficient math |
| `fpga_core.py` | `daq_core.py` | Persistent FPGA session, read/write, ramp, arb waveform, snapshot, monitor |
| `fpga_gui.py` | `daq_gui.py` | PyQt5 GUI with 8 tabs: Connection, X/Y/Z Feedback, Waveform, Outputs, All Registers, Monitor |
| `fpga_plot.py` | `daq_plot.py` | Dual-tab plotting: scrolling strip chart (Bead) + log-log PSD (Plot 0) |

### Data Flow

```
┌─────────────────────────┐
│   fpga_registers.py     │  Register definitions, coefficient formulas
│   (155 registers,       │  HOST_PARAMS (70+ freq/Q/ramp params)
│    70+ host params)     │  compute_coefficients()
└────────────┬────────────┘
             │ imports
┌────────────▼────────────┐
│     fpga_core.py        │  FPGAController — persistent session
│   connect / disconnect  │  read_register / write_register / read_all
│   change_pars / ramp    │  save_snapshot / load_snapshot
│   load_arb_waveform     │  save_sphere / load_sphere
│   start_monitor         │  background polling thread
└────────────┬────────────┘
             │ on_registers_updated callback
┌────────────▼────────────┐
│      fpga_gui.py        │  FPGAMainWindow — 8-tab PyQt5 interface
│   _build_pid_tab()      │  PID gains, filters, notch, host params
│   _build_outputs_tab()  │  EOM, COM, AO rotation controls
│   _on_change_pars()     │  Batch coefficient write
│   _on_boost()           │  Multiply gains by boost factor
└────────────┬────────────┘
             │ push_values()
┌────────────▼────────────┐
│      fpga_plot.py       │  FPGAPlotWidget — dual-tab plotting
│   "Bead" tab            │  Scrolling time-domain strip chart
│   "Plot 0" tab          │  Log-log PSD with window function selection
└─────────────────────────┘
```

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Persistent session** | FPGA session stays open for interactive parameter tuning (vs. DAQ's transient sessions) |
| **Read AND write** | Every writable register has a "Set" button; read-only indicators are greyed out |
| **Host-side parameters** | Enter frequencies (Hz) and Q factors; the GUI converts them to FPGA coefficients |
| **Change Pars** | Batch-write PID gains and computed filter coefficients per axis in one click |
| **Boost** | Temporarily multiply PG, DG, DG Band gains by a configurable factor (default 10x) |
| **Bead feedback** | Per-axis Normal / Inverted selector |
| **FFT / PSD** | Live power spectral density with selectable window (Hanning, Hamming, Blackman, Boxcar) |
| **Save / Load Sphere** | Quick-save all PID and host parameters for a sphere and restore later |
| **Snapshot save/load** | Dump / restore all register values to JSON |
| **Session log** | `fpga_session_log.jsonl` records every connect, write, and change_pars for audit |
| **Polling monitor** | Background thread reads all registers at configurable interval → live GUI updates |
| **Ramp Power** | Smoothly ramp `DC offset Z` from current to target in incremental steps |
| **Ramp Arb Drive** | Smoothly ramp arb waveform gains to target values |
| **Arb waveform loading** | Load waveform data from text file into 3 FPGA data buffers |
| **Rotation controls** | Reset voltage, AO4/AO5 revert, AO6/AO7 scan frequency booleans |
| **Simulation mode** | Full GUI operation without hardware when `nifpga` is not installed |

---

## GUI Tabs

| Tab | Contents |
|-----|----------|
| **Connection** | Bitfile path, resource string, poll interval, connect/disconnect, global registers, status log |
| **X Feedback** | X-axis PID gains, filter frequencies, notch filters, computed coefficients, Change Pars, Boost |
| **Y Feedback** | Same structure as X |
| **Z Feedback** | Same structure as Z, plus Ramp Power and before-chamber PID |
| **Waveform** | Arb waveform file loading, buffer gains, Ramp Arb Drive |
| **Outputs** | EOM controls, COM output cluster, AO channels 4-7 with rotation booleans |
| **All Registers** | Complete register list grouped by category with Read All / Write Changed |
| **Monitor** | Live time-domain strip chart (Bead) + PSD/FFT plot (Plot 0), channel checkboxes |

---

## Register Categories

| Category | Count | Description |
|----------|-------|-------------|
| Status / Timing | 3 | Stop, FPGA Error Out, Count(uSec) |
| Z Axis | ~40 | Setpoint, PID gains, limits, filters, accumulators, notch coefficients |
| Y Axis | ~35 | Same structure as Z (radial feedback via piezo Y) |
| X Axis | ~35 | Same structure as Z (radial feedback via piezo X) |
| Arbitrary Waveform | 10 | Gains (3 channels), buffers, addresses, steps per cycle |
| EOM | 8 | Amplitude, threshold, seed, offset, sine frequency, amplitude output |
| COM Output | 5 | Trigger, offset, amplitude, frequency, duty cycle |
| Global | 6 | Emergency thresholds, integral-gain disable, master enables |
| AO Channels (4-7) | ~19 | Frequency, amplitude, phase, reset per channel + rotation booleans |

> See [docs/FPGA_REGISTERS.md](docs/FPGA_REGISTERS.md) for the complete register table.

---

## Scripting Example

```python
from fpga_core import FPGAController, FPGAConfig

ctrl = FPGAController(FPGAConfig(
    bitfile=r"C:\path\to\bitfile.lvbitx",
    resource="PXI1Slot2",
))
ctrl.connect()

# Read / write individual registers
z_sp = ctrl.read_register("Z Setpoint")
ctrl.write_register("pg Z", 1.5)

# Read all ~155 registers at once
values = ctrl.read_all()

# Compute and write filter coefficients for Z axis
from fpga_registers import compute_coefficients
coeffs = compute_coefficients("Z", {"hp freq Z": 4000.0, "lp freq Z": 4000.0})
ctrl.write_many(coeffs)

# Ramp a register smoothly
ctrl.ramp_register("DC offset Z", target=3000.0, step=100.0, delay_s=0.05)

# Save / load complete register state
ctrl.save_snapshot("data/my_setup.json")
ctrl.load_snapshot("data/my_setup.json")

# Save / load sphere parameters (registers + host params)
ctrl.save_sphere("data/sphere_5um.json", host_params={"hp freq Z": 4000.0})
errors, host = ctrl.load_sphere("data/sphere_5um.json")

ctrl.disconnect()
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `nifpga` | ≥ 21.0 | NI FlexRIO FPGA driver (optional — simulation mode without it) |
| `numpy` | ≥ 1.24 | Numerical arrays, FFT/PSD computation |
| `PyQt5` | ≥ 5.15 | GUI framework |
| `matplotlib` | ≥ 3.7 | Real-time time-domain and frequency-domain plotting |

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/SETUP.md](docs/SETUP.md) | Installation, prerequisites, environment activation |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Step-by-step user guide for every GUI operation |
| [docs/FPGA_CORE.md](docs/FPGA_CORE.md) | `fpga_core.py` API reference |
| [docs/FPGA_REGISTERS.md](docs/FPGA_REGISTERS.md) | Complete register table, host params, coefficient formulas |
| [docs/FPGA_GUI.md](docs/FPGA_GUI.md) | `fpga_gui.py` architecture and tab-by-tab reference |
| [docs/FPGA_PLOT.md](docs/FPGA_PLOT.md) | `fpga_plot.py` plotting widget reference |
