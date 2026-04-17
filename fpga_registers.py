"""
fpga_registers.py

Register definitions for the NI PXIe-7856R FPGA module.
All register names must match exactly (including case and whitespace)
as they appear in the LabVIEW bitfile.

Each register is categorised for GUI organisation and annotated with
read/write capability.  The categories determine how the GUI groups
controls into tabs and collapsible sections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Register metadata
# ---------------------------------------------------------------------------

class Access(Enum):
    """Whether a register can be read, written, or both."""
    READ = "read"
    WRITE = "write"
    RW = "rw"


class Category(Enum):
    """Logical grouping for the GUI."""
    STATUS = "Status / Timing"
    Z_AXIS = "Z Axis"
    Y_AXIS = "Y Axis"
    X_AXIS = "X Axis"
    ARB_WAVEFORM = "Arbitrary Waveform"
    EOM = "EOM"
    COM_OUTPUT = "COM Output"
    GLOBAL = "Global"
    AO_CHANNELS = "AO Channels (4-7)"


@dataclass(frozen=True)
class RegisterDef:
    """Definition of a single FPGA register."""
    name: str
    category: Category
    access: Access = Access.RW
    is_bool: bool = False
    is_integer: bool = False   # True for I8/U8/I16/U16/I32/U32/I64/U64 FPGA types
    description: str = ""


# ---------------------------------------------------------------------------
# Full register table
# ---------------------------------------------------------------------------

REGISTERS: list[RegisterDef] = [

    # --- Status / timing ---
    RegisterDef("Stop",                   Category.STATUS, Access.RW, is_bool=True, description="Stop FPGA loop"),
    RegisterDef("FPGA Error Out",         Category.STATUS, Access.READ, description="Error indicator from FPGA"),
    RegisterDef("Count(uSec)",            Category.STATUS, Access.READ, is_integer=True, description="Microsecond tick counter"),

    # --- Z axis ---
    # Setpoints, gains, offsets, limits: I32 in LabVIEW VI (integer writes required)
    # Filter coefficients (HP/LP/Notch): FXP — written as float by compute_coefficients
    RegisterDef("Z Setpoint",             Category.Z_AXIS, is_integer=True, description="Z feedback setpoint"),
    RegisterDef("AI Z plot",              Category.Z_AXIS, Access.READ, description="Current Z sensor reading"),
    RegisterDef("Upper lim Z",            Category.Z_AXIS, is_integer=True, description="Upper saturation limit Z"),
    RegisterDef("Lower lim Z",            Category.Z_AXIS, is_integer=True, description="Lower saturation limit Z"),
    RegisterDef(" ig Z",                  Category.Z_AXIS, is_integer=True, description="Integral gain Z"),
    RegisterDef("fb Z plot",              Category.Z_AXIS, Access.READ, description="Feedback output Z"),
    RegisterDef("dg Z",                   Category.Z_AXIS, is_integer=True, description="Derivative gain Z"),
    RegisterDef("dg Z before",            Category.Z_AXIS, is_integer=True, description="Derivative gain Z (before chamber)"),
    RegisterDef(" ig Z before",           Category.Z_AXIS, is_integer=True, description="Integral gain Z (before chamber)"),
    RegisterDef("pg Z",                   Category.Z_AXIS, is_integer=True, description="Proportional gain Z"),
    RegisterDef("pg Z before",            Category.Z_AXIS, is_integer=True, description="Proportional gain Z (before chamber)"),
    RegisterDef("pg Z mod",               Category.Z_AXIS, is_integer=True, description="Proportional gain Z modulation"),
    RegisterDef("pz?",                    Category.Z_AXIS, Access.READ),
    RegisterDef("DC offset Z",            Category.Z_AXIS, is_integer=True, description="DC offset added to Z feedback"),
    RegisterDef("fb Z before chamber plot", Category.Z_AXIS, Access.READ, description="Feedback Z before chamber"),
    RegisterDef("tot_fb Z plot",          Category.Z_AXIS, Access.READ, description="Total feedback Z"),
    RegisterDef("Z before Setpoint",      Category.Z_AXIS, is_integer=True, description="Z before-chamber setpoint"),
    RegisterDef("AI Z before chamber plot", Category.Z_AXIS, Access.READ, description="Z sensor before chamber"),
    RegisterDef("Use Z PID before",       Category.Z_AXIS, is_bool=True, description="Enable before-chamber PID Z"),
    RegisterDef("HP Coeff Z",             Category.Z_AXIS, description="High-pass filter coeff Z"),
    RegisterDef("HP Coeff Z before",      Category.Z_AXIS, description="High-pass filter coeff Z (before)"),
    RegisterDef("dg band Z",              Category.Z_AXIS, is_integer=True, description="Derivative bandpass gain Z"),
    RegisterDef("dg band Z before",       Category.Z_AXIS, is_integer=True, description="Derivative bandpass gain Z (before)"),
    RegisterDef("HP Coeff band Z",        Category.Z_AXIS, description="HP bandpass coeff Z"),
    RegisterDef("LP Coeff band Z",        Category.Z_AXIS, description="LP bandpass coeff Z"),
    RegisterDef("LP Coeff band Z before", Category.Z_AXIS, description="LP bandpass coeff Z (before)"),
    RegisterDef("HP coeff band Z before", Category.Z_AXIS, description="HP bandpass coeff Z (before)"),
    RegisterDef("LP Coeff Z",             Category.Z_AXIS, description="Low-pass filter coeff Z"),
    RegisterDef("LP Coeff Z before",      Category.Z_AXIS, description="Low-pass filter coeff Z (before)"),
    RegisterDef("final filter coeff Z",   Category.Z_AXIS, description="Final output filter Z"),
    RegisterDef("final filter coeff Z before", Category.Z_AXIS, description="Final output filter Z (before)"),
    RegisterDef("Lower lim Z before",     Category.Z_AXIS, is_integer=True, description="Lower limit Z (before chamber)"),
    RegisterDef("Upper lim Z before",     Category.Z_AXIS, is_integer=True, description="Upper limit Z (before chamber)"),
    RegisterDef("activate COMz",          Category.Z_AXIS, is_bool=True, description="Activate COM Z output"),
    RegisterDef("dgz mod",                Category.Z_AXIS, is_integer=True, description="Derivative gain Z modulation"),
    RegisterDef("Reset z accum",          Category.Z_AXIS, is_bool=True, description="Reset Z accumulator"),
    RegisterDef("accum reset z1",         Category.Z_AXIS, is_bool=True, description="Reset accumulator z1"),
    RegisterDef("accum out z1",           Category.Z_AXIS, Access.READ, description="Accumulator z1 output"),
    RegisterDef("accurrm reset z2",       Category.Z_AXIS, is_bool=True, description="Reset accumulator z2"),
    RegisterDef("accum out z2",           Category.Z_AXIS, Access.READ, description="Accumulator z2 output"),
    RegisterDef("Notch coeff z 1",        Category.Z_AXIS, description="Notch filter 1 coeff Z"),
    RegisterDef("Notch coeff z 2",        Category.Z_AXIS, description="Notch filter 2 coeff Z"),
    RegisterDef("Notch coeff z 3",        Category.Z_AXIS, description="Notch filter 3 coeff Z"),
    RegisterDef("Notch coeff z 4",        Category.Z_AXIS, description="Notch filter 4 coeff Z"),

    # --- Y axis ---
    RegisterDef("Y Setpoint",             Category.Y_AXIS, is_integer=True, description="Y feedback setpoint"),
    RegisterDef("AI Y plot",              Category.Y_AXIS, Access.READ, description="Current Y sensor reading"),
    RegisterDef("pg Y",                   Category.Y_AXIS, is_integer=True, description="Proportional gain Y"),
    RegisterDef("Upper lim Y",            Category.Y_AXIS, is_integer=True, description="Upper saturation limit Y"),
    RegisterDef("Lower lim Y",            Category.Y_AXIS, is_integer=True, description="Lower saturation limit Y"),
    RegisterDef(" ig Y",                  Category.Y_AXIS, is_integer=True, description="Integral gain Y"),
    RegisterDef("fb Y plot",              Category.Y_AXIS, Access.READ, description="Feedback output Y"),
    RegisterDef("dg Y",                   Category.Y_AXIS, is_integer=True, description="Derivative gain Y"),
    RegisterDef("dg Y before",            Category.Y_AXIS, is_integer=True, description="Derivative gain Y (before chamber)"),
    RegisterDef(" ig Y before",           Category.Y_AXIS, is_integer=True, description="Integral gain Y (before chamber)"),
    RegisterDef("pg Y before",            Category.Y_AXIS, is_integer=True, description="Proportional gain Y (before chamber)"),
    RegisterDef("DC offset Y",            Category.Y_AXIS, is_integer=True, description="DC offset added to Y feedback"),
    RegisterDef("fb Y before chamber plot", Category.Y_AXIS, Access.READ, description="Feedback Y before chamber"),
    RegisterDef("tot_fb Y plot",          Category.Y_AXIS, Access.READ, description="Total feedback Y"),
    RegisterDef("Y before Setpoint",      Category.Y_AXIS, is_integer=True, description="Y before-chamber setpoint"),
    RegisterDef("AI Y before chamber plot", Category.Y_AXIS, Access.READ, description="Y sensor before chamber"),
    RegisterDef("Use Y PID before",       Category.Y_AXIS, is_bool=True, description="Enable before-chamber PID Y"),
    RegisterDef("HP Coeff Y",             Category.Y_AXIS, description="High-pass filter coeff Y"),
    RegisterDef("HP Coeff Y before",      Category.Y_AXIS, description="High-pass filter coeff Y (before)"),
    RegisterDef("dg band Y",              Category.Y_AXIS, is_integer=True, description="Derivative bandpass gain Y"),
    RegisterDef("dg band Y before",       Category.Y_AXIS, is_integer=True, description="Derivative bandpass gain Y (before)"),
    RegisterDef("HP Coeff band Y",        Category.Y_AXIS, description="HP bandpass coeff Y"),
    RegisterDef("LP Coeff band Y",        Category.Y_AXIS, description="LP bandpass coeff Y"),
    RegisterDef("LP Coeff band Y before", Category.Y_AXIS, description="LP bandpass coeff Y (before)"),
    RegisterDef("HP coeff band Y before", Category.Y_AXIS, description="HP bandpass coeff Y (before)"),
    RegisterDef("LP Coeff Y",             Category.Y_AXIS, description="Low-pass filter coeff Y"),
    RegisterDef("LP Coeff Y before",      Category.Y_AXIS, description="Low-pass filter coeff Y (before)"),
    RegisterDef("final filter coeff Y",   Category.Y_AXIS, description="Final output filter Y"),
    RegisterDef("final filter coeff Y before", Category.Y_AXIS, description="Final output filter Y (before)"),
    RegisterDef("Lower lim Y before",     Category.Y_AXIS, is_integer=True, description="Lower limit Y (before chamber)"),
    RegisterDef("Upper lim Y before",     Category.Y_AXIS, is_integer=True, description="Upper limit Y (before chamber)"),
    RegisterDef("dgy mod",                Category.Y_AXIS, is_integer=True, description="Derivative gain Y modulation"),
    RegisterDef("activate COMy",          Category.Y_AXIS, is_bool=True, description="Activate COM Y output"),
    RegisterDef("Reset y accum",          Category.Y_AXIS, is_bool=True, description="Reset Y accumulator"),
    RegisterDef("Notch coeff y 1",        Category.Y_AXIS, description="Notch filter 1 coeff Y"),
    RegisterDef("Notch coeff y 2",        Category.Y_AXIS, description="Notch filter 2 coeff Y"),
    RegisterDef("Notch coeff y 3",        Category.Y_AXIS, description="Notch filter 3 coeff Y"),
    RegisterDef("Notch coeff y 4",        Category.Y_AXIS, description="Notch filter 4 coeff Y"),

    # --- X axis ---
    RegisterDef("X Setpoint",             Category.X_AXIS, is_integer=True, description="X feedback setpoint"),
    RegisterDef("AI X plot",              Category.X_AXIS, Access.READ, description="Current X sensor reading"),
    RegisterDef("pg X",                   Category.X_AXIS, is_integer=True, description="Proportional gain X"),
    RegisterDef("Upper lim X",            Category.X_AXIS, is_integer=True, description="Upper saturation limit X"),
    RegisterDef("Lower lim X",            Category.X_AXIS, is_integer=True, description="Lower saturation limit X"),
    RegisterDef("ig X",                   Category.X_AXIS, is_integer=True, description="Integral gain X"),
    RegisterDef("fb X plot",              Category.X_AXIS, Access.READ, description="Feedback output X"),
    RegisterDef("dg X",                   Category.X_AXIS, is_integer=True, description="Derivative gain X"),
    RegisterDef("dg X before",            Category.X_AXIS, is_integer=True, description="Derivative gain X (before chamber)"),
    RegisterDef(" ig X before",           Category.X_AXIS, is_integer=True, description="Integral gain X (before chamber)"),
    RegisterDef("pg X before",            Category.X_AXIS, is_integer=True, description="Proportional gain X (before chamber)"),
    RegisterDef("DC offset X",            Category.X_AXIS, is_integer=True, description="DC offset added to X feedback"),
    RegisterDef("fb X before chamber plot", Category.X_AXIS, Access.READ, description="Feedback X before chamber"),
    RegisterDef("tot_fb X plot",          Category.X_AXIS, Access.READ, description="Total feedback X"),
    RegisterDef("X before Setpoint",      Category.X_AXIS, is_integer=True, description="X before-chamber setpoint"),
    RegisterDef("AI X before chamber plot", Category.X_AXIS, Access.READ, description="X sensor before chamber"),
    RegisterDef("Use X PID before",       Category.X_AXIS, is_bool=True, description="Enable before-chamber PID X"),
    RegisterDef("HP Coeff X",             Category.X_AXIS, description="High-pass filter coeff X"),
    RegisterDef("HP Coeff X before",      Category.X_AXIS, description="High-pass filter coeff X (before)"),
    RegisterDef("dg band X",              Category.X_AXIS, is_integer=True, description="Derivative bandpass gain X"),
    RegisterDef("dg band X before",       Category.X_AXIS, is_integer=True, description="Derivative bandpass gain X (before)"),
    RegisterDef("HP Coeff band X",        Category.X_AXIS, description="HP bandpass coeff X"),
    RegisterDef("LP Coeff band X",        Category.X_AXIS, description="LP bandpass coeff X"),
    RegisterDef("LP Coeff band X before", Category.X_AXIS, description="LP bandpass coeff X (before)"),
    RegisterDef("HP coeff band X before", Category.X_AXIS, description="HP bandpass coeff X (before)"),
    RegisterDef("LP Coeff X",             Category.X_AXIS, description="Low-pass filter coeff X"),
    RegisterDef("LP Coeff X before",      Category.X_AXIS, description="Low-pass filter coeff X (before)"),
    RegisterDef("final filter coeff X",   Category.X_AXIS, description="Final output filter X"),
    RegisterDef("final filter coeff X before", Category.X_AXIS, description="Final output filter X (before)"),
    RegisterDef("Lower lim X before",     Category.X_AXIS, is_integer=True, description="Lower limit X (before chamber)"),
    RegisterDef("Upper lim X before",     Category.X_AXIS, is_integer=True, description="Upper limit X (before chamber)"),
    RegisterDef("dgx mod",                Category.X_AXIS, is_integer=True, description="Derivative gain X modulation"),
    RegisterDef("activate COMx",          Category.X_AXIS, is_bool=True, description="Activate COM X output"),
    RegisterDef("Reset x accum",          Category.X_AXIS, is_bool=True, description="Reset X accumulator"),
    RegisterDef("Notch coeff x 1",        Category.X_AXIS, description="Notch filter 1 coeff X"),
    RegisterDef("Notch coeff x 2",        Category.X_AXIS, description="Notch filter 2 coeff X"),
    RegisterDef("Notch coeff x 3",        Category.X_AXIS, description="Notch filter 3 coeff X"),
    RegisterDef("Notch coeff x 4",        Category.X_AXIS, description="Notch filter 4 coeff X"),

    # --- Arbitrary waveform ---
    RegisterDef("Arb gain (ch0)",         Category.ARB_WAVEFORM, is_integer=True, description="Arb waveform gain ch0"),
    RegisterDef("Arb gain (ch1)",         Category.ARB_WAVEFORM, is_integer=True, description="Arb waveform gain ch1"),
    RegisterDef("Arb gain (ch2)",         Category.ARB_WAVEFORM, is_integer=True, description="Arb waveform gain ch2"),
    RegisterDef("write_address",          Category.ARB_WAVEFORM, is_integer=True, description="Waveform write address"),
    RegisterDef("data_buffer_1",          Category.ARB_WAVEFORM, description="Data buffer 1"),
    RegisterDef("data_buffer2",           Category.ARB_WAVEFORM, description="Data buffer 2"),
    RegisterDef("data_buffer3",           Category.ARB_WAVEFORM, description="Data buffer 3"),
    RegisterDef("Arb steps per cycle",    Category.ARB_WAVEFORM, is_integer=True, description="Steps per waveform cycle"),
    RegisterDef("ready_to_write",         Category.ARB_WAVEFORM, Access.READ, description="Buffer write ready"),
    RegisterDef("written_address",        Category.ARB_WAVEFORM, Access.READ, is_integer=True, description="Last written address"),

    # --- EOM ---
    RegisterDef("EOM_amplitude",          Category.EOM, is_integer=True, description="EOM drive amplitude"),
    RegisterDef("EOM_threshold",          Category.EOM, is_integer=True, description="EOM threshold"),
    RegisterDef("EOM reset",              Category.EOM, is_bool=True, description="Reset EOM"),
    RegisterDef("EOM_seed",               Category.EOM, is_integer=True, description="EOM random seed"),
    RegisterDef("EOM_offset",             Category.EOM, is_integer=True, description="EOM DC offset"),
    RegisterDef("eom sine frequency (periods/tick)", Category.EOM, is_integer=True, description="EOM sine frequency"),
    RegisterDef("Amplitude_sine_EOM",     Category.EOM, is_integer=True, description="Sine amplitude for EOM"),
    RegisterDef("EOM_amplitude_out",      Category.EOM, Access.READ, description="EOM amplitude output indicator"),

    # --- COM output ---
    RegisterDef("Trigger for COM out",    Category.COM_OUTPUT, is_bool=True, description="Trigger COM output"),
    RegisterDef("offset",                 Category.COM_OUTPUT, is_integer=True, description="COM output offset"),
    RegisterDef("amplitude",              Category.COM_OUTPUT, is_integer=True, description="COM output amplitude"),
    RegisterDef("frequency (periods/tick)", Category.COM_OUTPUT, is_integer=True, description="COM output frequency"),
    RegisterDef("duty cycle (periods)",   Category.COM_OUTPUT, is_integer=True, description="COM output duty cycle"),

    # --- Global ---
    RegisterDef("Big Number",             Category.GLOBAL, Access.READ, is_integer=True, description="Counter tick"),
    RegisterDef("X_emergency_threshould", Category.GLOBAL, is_integer=True, description="X emergency threshold"),
    RegisterDef("Y_emergency_threshould", Category.GLOBAL, is_integer=True, description="Y emergency threshold"),
    RegisterDef("No_integral_gain",       Category.GLOBAL, is_bool=True, description="Disable integral gain globally"),
    RegisterDef("master x",              Category.GLOBAL, is_bool=True, description="Master enable X feedback"),
    RegisterDef("master y",              Category.GLOBAL, is_bool=True, description="Master enable Y feedback"),

    # --- AO channels 4-7 / Rotation Control ---
    RegisterDef("Reset voltage",           Category.AO_CHANNELS, is_bool=True, description="Reset AO voltage outputs"),
    RegisterDef("If revert AO4 and AO5",  Category.AO_CHANNELS, is_bool=True, description="Revert AO4 and AO5 outputs"),
    RegisterDef("If scan frequency (AO6 and AO7)?", Category.AO_CHANNELS, is_bool=True, description="Scan frequency on AO6 and AO7"),
    RegisterDef("frequency AO4",          Category.AO_CHANNELS, is_integer=True, description="AO4 frequency"),
    RegisterDef("reset AO4",              Category.AO_CHANNELS, is_bool=True, description="Reset AO4"),
    RegisterDef("phase offset AO4",       Category.AO_CHANNELS, is_integer=True, description="AO4 phase offset"),
    RegisterDef("Amplitude AO4",          Category.AO_CHANNELS, is_integer=True, description="AO4 amplitude"),
    RegisterDef("frequency AO5",          Category.AO_CHANNELS, is_integer=True, description="AO5 frequency"),
    RegisterDef("reset AO5",              Category.AO_CHANNELS, is_bool=True, description="Reset AO5"),
    RegisterDef("phase offset AO5",       Category.AO_CHANNELS, is_integer=True, description="AO5 phase offset"),
    RegisterDef("Amplitude AO5",          Category.AO_CHANNELS, is_integer=True, description="AO5 amplitude"),
    RegisterDef("frequency AO6",          Category.AO_CHANNELS, is_integer=True, description="AO6 frequency"),
    RegisterDef("reset AO6",              Category.AO_CHANNELS, is_bool=True, description="Reset AO6"),
    RegisterDef("phase offset AO6",       Category.AO_CHANNELS, is_integer=True, description="AO6 phase offset"),
    RegisterDef("Amplitude AO6",          Category.AO_CHANNELS, is_integer=True, description="AO6 amplitude"),
    RegisterDef("frequency AO7",          Category.AO_CHANNELS, is_integer=True, description="AO7 frequency"),
    RegisterDef("reset AO7",              Category.AO_CHANNELS, is_bool=True, description="Reset AO7"),
    RegisterDef("phase offset AO7",       Category.AO_CHANNELS, is_integer=True, description="AO7 phase offset"),
    RegisterDef("Amplitude AO7",          Category.AO_CHANNELS, is_integer=True, description="AO7 amplitude"),
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

REGISTER_MAP: dict[str, RegisterDef] = {r.name: r for r in REGISTERS}
ALL_NAMES: list[str] = [r.name for r in REGISTERS]
DEFAULTS: dict[str, float] = {r.name: 0.0 for r in REGISTERS}


def names_by_category(cat: Category) -> list[str]:
    """Return register names belonging to *cat*, in definition order."""
    return [r.name for r in REGISTERS if r.category == cat]


def writable_registers() -> list[RegisterDef]:
    """Return registers that can be written to."""
    return [r for r in REGISTERS if r.access in (Access.WRITE, Access.RW)]


def readable_registers() -> list[RegisterDef]:
    """Return registers that can be read."""
    return [r for r in REGISTERS if r.access in (Access.READ, Access.RW)]


# ---------------------------------------------------------------------------
# FPGA sample rate
# ---------------------------------------------------------------------------

FPGA_SAMPLE_RATE = 100_000  # Hz — derived from Count(uSec) = 10


# ---------------------------------------------------------------------------
# Host-side parameters
# ---------------------------------------------------------------------------
# These are GUI-only controls (frequencies, Q-values, ramp targets) that the
# host converts into FPGA register values before writing.  They are never
# read from the FPGA — they exist only in the host UI and session files.

@dataclass(frozen=True)
class HostParam:
    """A host-side parameter displayed in the GUI."""
    name: str
    category: Category
    default: float = 0.0
    description: str = ""


HOST_PARAMS: list[HostParam] = [

    # --- X axis filter / notch parameters ---
    HostParam("hp freq X",              Category.X_AXIS, 400.0,  "HP cutoff X (Hz)"),
    HostParam("lp freq X",              Category.X_AXIS, 130.0,  "LP cutoff X (Hz)"),
    HostParam("LP FF X",                Category.X_AXIS, 1300.0, "Final LP filter X (Hz)"),
    HostParam("hp freq X before",       Category.X_AXIS, 5000.0, "HP cutoff X before (Hz)"),
    HostParam("lp freq X before",       Category.X_AXIS, 5000.0, "LP cutoff X before (Hz)"),
    HostParam("hp freq bandX",          Category.X_AXIS, 5000.0, "HP band freq X (Hz)"),
    HostParam("lp freq bandX",          Category.X_AXIS, 5000.0, "LP band freq X (Hz)"),
    HostParam("hp freq X band before",  Category.X_AXIS, 5000.0, "HP band freq X before (Hz)"),
    HostParam("lp freq X band before",  Category.X_AXIS, 5000.0, "LP band freq X before (Hz)"),
    HostParam("notch freq 1 x",         Category.X_AXIS, 960.0,  "Notch 1 freq X (Hz)"),
    HostParam("notch Q 1 x",            Category.X_AXIS, 1.0,    "Notch 1 Q X"),
    HostParam("notch freq 2 x",         Category.X_AXIS, 1309.0, "Notch 2 freq X (Hz)"),
    HostParam("notch Q 2 x",            Category.X_AXIS, 2.0,    "Notch 2 Q X"),
    HostParam("notch freq 3 x",         Category.X_AXIS, 1000.0, "Notch 3 freq X (Hz)"),
    HostParam("notch Q 3 x",            Category.X_AXIS, 5.0,    "Notch 3 Q X"),
    HostParam("notch freq 4 x",         Category.X_AXIS, 1000.0, "Notch 4 freq X (Hz)"),
    HostParam("notch Q 4 x",            Category.X_AXIS, 5.0,    "Notch 4 Q X"),

    # --- Y axis filter / notch parameters ---
    HostParam("hp freq Y",              Category.Y_AXIS, 400.0,  "HP cutoff Y (Hz)"),
    HostParam("lp freq Y",              Category.Y_AXIS, 130.0,  "LP cutoff Y (Hz)"),
    HostParam("LP FF Y",                Category.Y_AXIS, 1300.0, "Final LP filter Y (Hz)"),
    HostParam("hp freq Y before",       Category.Y_AXIS, 5000.0, "HP cutoff Y before (Hz)"),
    HostParam("lp freq Y before",       Category.Y_AXIS, 5000.0, "LP cutoff Y before (Hz)"),
    HostParam("hp freq bandY",          Category.Y_AXIS, 5000.0, "HP band freq Y (Hz)"),
    HostParam("lp freq bandY",          Category.Y_AXIS, 5000.0, "LP band freq Y (Hz)"),
    HostParam("hp freq Y band before",  Category.Y_AXIS, 5000.0, "HP band freq Y before (Hz)"),
    HostParam("lp freq Y band before",  Category.Y_AXIS, 5000.0, "LP band freq Y before (Hz)"),
    HostParam("notch freq 1 y",         Category.Y_AXIS, 267.0,  "Notch 1 freq Y (Hz)"),
    HostParam("notch Q 1 y",            Category.Y_AXIS, 1.0,    "Notch 1 Q Y"),
    HostParam("notch freq 2 y",         Category.Y_AXIS, 340.0,  "Notch 2 freq Y (Hz)"),
    HostParam("notch Q 2 y",            Category.Y_AXIS, 2.0,    "Notch 2 Q Y"),
    HostParam("notch freq 3 y",         Category.Y_AXIS, 1000.0, "Notch 3 freq Y (Hz)"),
    HostParam("notch Q 3 y",            Category.Y_AXIS, 5.0,    "Notch 3 Q Y"),
    HostParam("notch freq 4 y",         Category.Y_AXIS, 1000.0, "Notch 4 freq Y (Hz)"),
    HostParam("notch Q 4 y",            Category.Y_AXIS, 5.0,    "Notch 4 Q Y"),

    # --- Z axis filter / notch parameters ---
    HostParam("hp freq Z",              Category.Z_AXIS, 4000.0, "HP cutoff Z (Hz)"),
    HostParam("lp freq Z",              Category.Z_AXIS, 4000.0, "LP cutoff Z (Hz)"),
    HostParam("LP FF Z",                Category.Z_AXIS, 4200.0, "Final LP filter Z (Hz)"),
    HostParam("LP FF Z before",         Category.Z_AXIS, 5000.0, "Final LP filter Z before (Hz)"),
    HostParam("hp freq Z before",       Category.Z_AXIS, 5000.0, "HP cutoff Z before (Hz)"),
    HostParam("lp freq Z before",       Category.Z_AXIS, 5000.0, "LP cutoff Z before (Hz)"),
    HostParam("hp freq bandZ",          Category.Z_AXIS, 5000.0, "HP band freq Z (Hz)"),
    HostParam("lp freq bandZ",          Category.Z_AXIS, 49.0,   "LP band freq Z (Hz)"),
    HostParam("hp freq band Z before",  Category.Z_AXIS, 5000.0, "HP band freq Z before (Hz)"),
    HostParam("lp freq band Z before",  Category.Z_AXIS, 5000.0, "LP band freq Z before (Hz)"),
    HostParam("notch freq 1 z",         Category.Z_AXIS, 960.0,  "Notch 1 freq Z (Hz)"),
    HostParam("notch Q 1 z",            Category.Z_AXIS, 4.0,    "Notch 1 Q Z"),
    HostParam("notch freq 2 z",         Category.Z_AXIS, 1309.0, "Notch 2 freq Z (Hz)"),
    HostParam("notch Q 2 z",            Category.Z_AXIS, 4.0,    "Notch 2 Q Z"),
    HostParam("notch freq 3 z",         Category.Z_AXIS, 1000.0, "Notch 3 freq Z (Hz)"),
    HostParam("notch Q 3 z",            Category.Z_AXIS, 5.0,    "Notch 3 Q Z"),
    HostParam("notch freq 4 z",         Category.Z_AXIS, 1000.0, "Notch 4 freq Z (Hz)"),
    HostParam("notch Q 4 z",            Category.Z_AXIS, 5.0,    "Notch 4 Q Z"),

    # --- Z ramp-power parameters (host-side ramp) ---
    HostParam("End value power",        Category.Z_AXIS, 3000.0, "Power ramp target value"),
    HostParam("Step power",             Category.Z_AXIS, 0.0,    "Power ramp step size"),
    HostParam("Delay Time (s) power",   Category.Z_AXIS, 0.05,   "Power ramp delay (s)"),

    # --- Arb waveform ramp parameters ---
    HostParam("End value arb (ch0)",    Category.ARB_WAVEFORM, 0.0,     "Arb ramp target ch0"),
    HostParam("End value arb (ch1)",    Category.ARB_WAVEFORM, 0.10000, "Arb ramp target ch1"),
    HostParam("Step arb (ch0)",         Category.ARB_WAVEFORM, 0.0,     "Arb ramp step ch0"),
    HostParam("Step arb (ch1)",         Category.ARB_WAVEFORM, 0.0,     "Arb ramp step ch1"),
    HostParam("Delay Time (s) arb",     Category.ARB_WAVEFORM, 0.001,   "Arb ramp delay (s)"),
    HostParam("z arb scale",            Category.ARB_WAVEFORM, 0.0,     "Z arb waveform scale"),

    # --- EOM ---
    HostParam("Frequency_sine_EOM (Hz)", Category.EOM, 0.0, "EOM sine frequency (Hz)"),

    # --- COM output ---
    HostParam("frequency (kHz)",        Category.COM_OUTPUT, 0.0015, "COM output frequency (kHz)"),

    # --- AO channels (Hz input) ---
    HostParam("frequency AO4 (Hz)",     Category.AO_CHANNELS, 0.0,  "AO4 frequency (Hz)"),
    HostParam("frequency AO5 (Hz)",     Category.AO_CHANNELS, 7.0,  "AO5 frequency (Hz)"),
    HostParam("frequency AO6 (Hz)",     Category.AO_CHANNELS, 80.0, "AO6 frequency (Hz)"),
    HostParam("frequency AO7 (Hz)",     Category.AO_CHANNELS, 80.0, "AO7 frequency (Hz)"),
]

HOST_PARAM_MAP: dict[str, HostParam] = {h.name: h for h in HOST_PARAMS}
HOST_PARAM_DEFAULTS: dict[str, float] = {h.name: h.default for h in HOST_PARAMS}


def host_params_by_category(cat: Category) -> list[HostParam]:
    """Return host parameters belonging to *cat*, in definition order."""
    return [h for h in HOST_PARAMS if h.category == cat]


# ---------------------------------------------------------------------------
# Filter coefficient conversions
# ---------------------------------------------------------------------------
# These functions implement standard first-order IIR coefficient formulas.
# The FPGA representation may use fixed-point scaling — adjust if needed.

def freq_to_lp_coeff(freq_hz: float,
                     sample_rate: float = FPGA_SAMPLE_RATE) -> float:
    """LP IIR coefficient: alpha = exp(-2*pi*f/fs)."""
    if freq_hz <= 0 or freq_hz >= sample_rate / 2:
        return 0.0
    return math.exp(-2.0 * math.pi * freq_hz / sample_rate)


def freq_to_hp_coeff(freq_hz: float,
                     sample_rate: float = FPGA_SAMPLE_RATE) -> float:
    """HP IIR coefficient: alpha = exp(-2*pi*f/fs)."""
    if freq_hz <= 0 or freq_hz >= sample_rate / 2:
        return 0.0
    return math.exp(-2.0 * math.pi * freq_hz / sample_rate)


def freq_q_to_notch_coeff(freq_hz: float, q: float,
                           sample_rate: float = FPGA_SAMPLE_RATE) -> float:
    """Notch coefficient from center frequency and Q.

    Returns 2*r*cos(2*pi*f0/fs) where r = 1 - pi*(f0/Q)/fs.
    The exact meaning depends on the FPGA notch-filter implementation.
    """
    if freq_hz <= 0 or q <= 0:
        return 0.0
    bw = freq_hz / q
    r = max(0.0, 1.0 - math.pi * bw / sample_rate)
    return 2.0 * r * math.cos(2.0 * math.pi * freq_hz / sample_rate)


def hz_to_periods_per_tick(freq_hz: float,
                           sample_rate: float = FPGA_SAMPLE_RATE) -> float:
    """Convert Hz to periods-per-tick (fs / f)."""
    if freq_hz <= 0:
        return 0.0
    return sample_rate / freq_hz


def compute_coefficients(axis: str, host_params: dict[str, float],
                         sample_rate: float = FPGA_SAMPLE_RATE) -> dict[str, float]:
    """Compute FPGA filter-coefficient registers from host parameters.

    Parameters
    ----------
    axis : "X", "Y", or "Z"
    host_params : dict mapping host-param names to their current values
    sample_rate : FPGA loop rate in Hz

    Returns
    -------
    dict mapping FPGA register names to computed coefficient values
    """
    a = axis.upper()
    al = axis.lower()
    result: dict[str, float] = {}

    # HP / LP filter coefficients
    result[f"HP Coeff {a}"] = freq_to_hp_coeff(
        host_params.get(f"hp freq {a}", 0), sample_rate)
    result[f"LP Coeff {a}"] = freq_to_lp_coeff(
        host_params.get(f"lp freq {a}", 0), sample_rate)
    result[f"HP Coeff {a} before"] = freq_to_hp_coeff(
        host_params.get(f"hp freq {a} before", 0), sample_rate)
    result[f"LP Coeff {a} before"] = freq_to_lp_coeff(
        host_params.get(f"lp freq {a} before", 0), sample_rate)

    # Band-pass filter coefficients
    result[f"HP Coeff band {a}"] = freq_to_hp_coeff(
        host_params.get(f"hp freq band{a}", 0), sample_rate)
    result[f"LP Coeff band {a}"] = freq_to_lp_coeff(
        host_params.get(f"lp freq band{a}", 0), sample_rate)

    # Band before-chamber (host-param name differs between X/Y and Z)
    hp_band_before = host_params.get(
        f"hp freq {a} band before",
        host_params.get(f"hp freq band {a} before", 0))
    result[f"HP coeff band {a} before"] = freq_to_hp_coeff(
        hp_band_before, sample_rate)

    lp_band_before = host_params.get(
        f"lp freq {a} band before",
        host_params.get(f"lp freq band {a} before", 0))
    result[f"LP Coeff band {a} before"] = freq_to_lp_coeff(
        lp_band_before, sample_rate)

    # Final LP filter
    result[f"final filter coeff {a}"] = freq_to_lp_coeff(
        host_params.get(f"LP FF {a}", 0), sample_rate)
    if a == "Z":
        result["final filter coeff Z before"] = freq_to_lp_coeff(
            host_params.get("LP FF Z before", 0), sample_rate)

    # Notch filters (4 per axis)
    for i in range(1, 5):
        f = host_params.get(f"notch freq {i} {al}", 0)
        q = host_params.get(f"notch Q {i} {al}", 1)
        result[f"Notch coeff {al} {i}"] = freq_q_to_notch_coeff(f, q, sample_rate)

    return result
