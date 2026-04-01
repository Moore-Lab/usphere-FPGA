# Setup Guide

Installation and environment configuration for `usphere-FPGA`.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Clone the Repository](#clone-the-repository)
3. [Automated Install (Recommended)](#automated-install-recommended)
4. [Manual Install](#manual-install)
5. [Activating the Virtual Environment](#activating-the-virtual-environment)
6. [Verifying NI Hardware](#verifying-ni-hardware)
7. [Simulation Mode](#simulation-mode)
8. [Launching the GUI](#launching-the-gui)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Python 3.10+** | CPython (not Anaconda). 3.11 or 3.12 recommended. |
| **NI-RIO driver** | Required only for real hardware. Install from [ni.com/downloads](https://www.ni.com/en/support/downloads/drivers/download.ni-rio.html). Version ≥ 21.0. |
| **NI MAX** (Measurement & Automation Explorer) | Confirms the PXIe-7856R is visible as a resource (e.g. `PXI1Slot2`). |
| **LabVIEW bitfile** | `Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx` — must already be compiled. The Python interface does **not** compile bitfiles. |
| **OS** | Windows 10/11 (primary target — NI drivers are Windows-only). Linux/macOS work in simulation mode. |

---

## Clone the Repository

```bash
git clone <repository-url>
cd usphere-FPGA
```

The working directory should look like this:

```
usphere-FPGA/
├── fpga_registers.py
├── fpga_core.py
├── fpga_gui.py
├── fpga_plot.py
├── install_deps.py
├── requirements.txt
├── README.md
├── assets/
├── data/
└── docs/
```

---

## Automated Install (Recommended)

The included `install_deps.py` script creates a virtual environment and
installs all dependencies in one step:

```bash
python install_deps.py
```

This will:

1. Create a `.venv/` directory in the project root (if it does not already
   exist).
2. Upgrade `pip` inside the virtual environment.
3. Install every package listed in `requirements.txt`:
   - `nifpga >= 21.0.0`
   - `numpy  >= 1.24.0`
   - `PyQt5  >= 5.15.0`
   - `matplotlib >= 3.7.0`

### Skip Virtual Environment

If you prefer to install into your current Python environment (system or
conda), pass `--no-venv`:

```bash
python install_deps.py --no-venv
```

---

## Manual Install

If you prefer doing it yourself:

```bash
# Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Activating the Virtual Environment

You must activate the virtual environment in every new terminal session before
running the GUI or any scripts.

### Windows (PowerShell)

```powershell
.venv\Scripts\Activate.ps1
```

> **Tip:** If PowerShell blocks execution, run:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

### Windows (Command Prompt)

```cmd
.venv\Scripts\activate.bat
```

### Linux / macOS

```bash
source .venv/bin/activate
```

Your prompt should show `(.venv)` when the environment is active.

---

## Verifying NI Hardware

Before running the GUI against real hardware:

1. **Open NI MAX** (search for "Measurement & Automation Explorer" in Start).
2. Expand **Remote Systems** or **My System → Devices and Interfaces**.
3. Confirm the **PXIe-7856R** appears. Note its **resource string** — usually
   `PXI1Slot2` or similar (e.g. `PXI1Slot4`).
4. Right-click the device and run a **Self-Test** to confirm the driver can
   communicate with it.

The resource string and bitfile path are entered on the **Connection** tab of
the GUI.

---

## Simulation Mode

When the `nifpga` package **cannot be imported** (e.g. on a laptop without NI
drivers), the GUI starts in **simulation mode** automatically:

- All 155 registers are backed by an in-memory dictionary.
- `read_register()` / `write_register()` / `read_all()` work normally.
- Change Pars, Boost, Ramp, Snapshot save/load all function.
- The Monitor tab generates zero-valued data (no real ADC signals).
- No FPGA hardware is accessed.

This allows full GUI development, testing, and demonstration without a PXIe
chassis. No additional configuration is needed — simply launch the GUI and
it will print a simulation-mode banner in the status log.

---

## Launching the GUI

```bash
python fpga_gui.py
```

The main window opens with 8 tabs. See [USER_GUIDE.md](USER_GUIDE.md) for a
walkthrough of each tab.

### Command-Line Launch Alternatives

```bash
# Explicit Python path (useful if multiple Pythons installed)
.venv\Scripts\python.exe fpga_gui.py

# From a different directory
python C:\path\to\usphere-FPGA\fpga_gui.py
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'nifpga'`

| Situation | Solution |
|-----------|----------|
| No NI drivers installed | This is expected — the GUI will start in **simulation mode**. |
| NI drivers installed but import fails | Make sure you installed `nifpga` in the active environment: `pip install nifpga`. |
| Wrong Python version | `nifpga` requires CPython 3.8+. Check with `python --version`. |

### `ModuleNotFoundError: No module named 'PyQt5'`

You are running outside the virtual environment. Activate it first:
```bash
.venv\Scripts\activate   # Windows
source .venv/bin/activate # Linux/macOS
```

### PyQt5 fails to install on Linux

On Ubuntu/Debian you may need system Qt libraries:
```bash
sudo apt install python3-pyqt5
```

### FPGA connection fails with "resource not found"

1. Open NI MAX and verify the device resource string.
2. Ensure the resource string in the GUI's Connection tab matches exactly
   (e.g. `PXI1Slot2`).
3. Confirm no other application (LabVIEW, another Python script) has an
   open session to the same FPGA.

### FPGA connection fails with "bitfile not found"

1. Check the bitfile path in the Connection tab — it must point to the
   `.lvbitx` file on disk.
2. Use an absolute path (e.g. `C:\NI\Bitfiles\Microspherefeedb_...DCM.lvbitx`).
3. Verify the file exists: `Test-Path "C:\path\to\bitfile.lvbitx"`.

### PowerShell script execution is disabled

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### GUI looks blurry on high-DPI displays

Add this before importing PyQt5 (at the top of `fpga_gui.py`):
```python
import os
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
```

### PSD plot is flat / all zeros

- The Monitor must be running (click **Start Monitor** on the Connection tab).
- At least one channel checkbox must be enabled on the Monitor tab.
- In simulation mode, all signals are zero — this is expected.

---

## Directory Structure After Install

```
usphere-FPGA/
├── .venv/                ← Created by install_deps.py (gitignored)
│   ├── Lib/
│   ├── Scripts/          ← python.exe, pip.exe, activate
│   └── ...
├── data/                 ← Snapshots, sphere saves (created at runtime)
│   ├── snapshot_*.json
│   └── sphere_*.json
├── fpga_session_log.jsonl  ← Session audit log (created at runtime)
├── fpga_registers.py
├── fpga_core.py
├── fpga_gui.py
├── fpga_plot.py
├── install_deps.py
├── requirements.txt
├── README.md
├── assets/
└── docs/
```

---

*See also: [README.md](../README.md) · [USER_GUIDE.md](USER_GUIDE.md) · [FPGA_CORE.md](FPGA_CORE.md)*
