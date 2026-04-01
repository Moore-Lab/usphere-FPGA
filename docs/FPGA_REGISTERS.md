# fpga_registers.py — Register Definitions & Coefficient Conversion

`fpga_registers.py` is the single source of truth for every FPGA register,
every host-side parameter, and the mathematical functions that convert
human-readable frequencies and Q values into FPGA filter coefficients.

> **Critical:** All register names must match the LabVIEW bitfile **exactly**,
> including case, whitespace, and punctuation.  Some registers have leading
> spaces (e.g. `" ig Z"`), inconsistent casing (e.g. `"HP coeff band Z before"`
> vs `"HP Coeff band Z"`), or question marks (e.g. `"pz?"`).

---

## Table of Contents

1. [Data Structures](#data-structures)
2. [Complete Register Table](#complete-register-table)
3. [Lookup Helpers](#lookup-helpers)
4. [FPGA Sample Rate](#fpga-sample-rate)
5. [Host-Side Parameters](#host-side-parameters)
6. [Complete Host Parameter Table](#complete-host-parameter-table)
7. [Coefficient Conversion Functions](#coefficient-conversion-functions)
8. [compute_coefficients()](#compute_coefficients)

---

## Data Structures

### Access (Enum)

Controls whether a register can be read, written, or both.

| Value | Meaning |
|-------|---------|
| `Access.READ` | Indicator — read-only from the host |
| `Access.WRITE` | Control — write-only (rare) |
| `Access.RW` | Control & indicator — can be read and written |

### Category (Enum)

Logical grouping used by the GUI for tab/section layout.

| Value | Display name | GUI location |
|-------|-------------|-------------|
| `Category.STATUS` | Status / Timing | Connection tab |
| `Category.Z_AXIS` | Z Axis | Z Feedback tab |
| `Category.Y_AXIS` | Y Axis | Y Feedback tab |
| `Category.X_AXIS` | X Axis | X Feedback tab |
| `Category.ARB_WAVEFORM` | Arbitrary Waveform | Waveform tab |
| `Category.EOM` | EOM | Outputs tab |
| `Category.COM_OUTPUT` | COM Output | Outputs tab |
| `Category.GLOBAL` | Global | Connection tab |
| `Category.AO_CHANNELS` | AO Channels (4-7) | Outputs tab |

### RegisterDef (dataclass, frozen)

```python
@dataclass(frozen=True)
class RegisterDef:
    name: str            # Must match LabVIEW bitfile exactly
    category: Category   # Logical group for GUI layout
    access: Access       # READ, WRITE, or RW (default: RW)
    is_bool: bool        # True for boolean/checkbox registers (default: False)
    description: str     # Human-readable label (default: "")
```

### HostParam (dataclass, frozen)

```python
@dataclass(frozen=True)
class HostParam:
    name: str            # Display name in the GUI
    category: Category   # Which axis/section it belongs to
    default: float       # Default value (default: 0.0)
    description: str     # Tooltip text (default: "")
```

---

## Complete Register Table

### Status / Timing (3 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Stop` | RW | Yes | Stop FPGA loop |
| `FPGA Error Out` | READ | No | Error indicator from FPGA |
| `Count(uSec)` | READ | No | Microsecond tick counter |

### Z Axis (~40 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Z Setpoint` | RW | No | Z feedback setpoint |
| `AI Z plot` | READ | No | Current Z sensor reading |
| `Upper lim Z` | RW | No | Upper saturation limit Z |
| `Lower lim Z` | RW | No | Lower saturation limit Z |
| ` ig Z` | RW | No | Integral gain Z *(note leading space)* |
| `fb Z plot` | READ | No | Feedback output Z |
| `dg Z` | RW | No | Derivative gain Z |
| `dg Z before` | RW | No | Derivative gain Z (before chamber) |
| ` ig Z before` | RW | No | Integral gain Z (before chamber) *(leading space)* |
| `pg Z` | RW | No | Proportional gain Z |
| `pg Z before` | RW | No | Proportional gain Z (before chamber) |
| `pg Z mod` | RW | No | Proportional gain Z modulation |
| `pz?` | READ | No | *(undefined indicator)* |
| `DC offset Z` | RW | No | DC offset added to Z feedback |
| `fb Z before chamber plot` | READ | No | Feedback Z before chamber |
| `tot_fb Z plot` | READ | No | Total feedback Z |
| `Z before Setpoint` | RW | No | Z before-chamber setpoint |
| `AI Z before chamber plot` | READ | No | Z sensor before chamber |
| `Use Z PID before` | RW | Yes | Enable before-chamber PID Z |
| `HP Coeff Z` | RW | No | High-pass filter coefficient Z |
| `HP Coeff Z before` | RW | No | High-pass filter coefficient Z (before) |
| `dg band Z` | RW | No | Derivative bandpass gain Z |
| `dg band Z before` | RW | No | Derivative bandpass gain Z (before) |
| `HP Coeff band Z` | RW | No | HP bandpass coefficient Z |
| `LP Coeff band Z` | RW | No | LP bandpass coefficient Z |
| `LP Coeff band Z before` | RW | No | LP bandpass coefficient Z (before) |
| `HP coeff band Z before` | RW | No | HP bandpass coefficient Z (before) *(lowercase 'c')* |
| `LP Coeff Z` | RW | No | Low-pass filter coefficient Z |
| `LP Coeff Z before` | RW | No | Low-pass filter coefficient Z (before) |
| `final filter coeff Z` | RW | No | Final output filter Z |
| `final filter coeff Z before` | RW | No | Final output filter Z (before) |
| `Lower lim Z before` | RW | No | Lower limit Z (before chamber) |
| `Upper lim Z before` | RW | No | Upper limit Z (before chamber) |
| `activate COMz` | RW | Yes | Activate COM Z output |
| `dgz mod` | RW | No | Derivative gain Z modulation |
| `Reset z accum` | RW | Yes | Reset Z accumulator |
| `accum reset z1` | RW | Yes | Reset accumulator z1 |
| `accum out z1` | READ | No | Accumulator z1 output |
| `accurrm reset z2` | RW | Yes | Reset accumulator z2 *(note typo in name)* |
| `accum out z2` | READ | No | Accumulator z2 output |
| `Notch coeff z 1` | RW | No | Notch filter 1 coefficient Z |
| `Notch coeff z 2` | RW | No | Notch filter 2 coefficient Z |
| `Notch coeff z 3` | RW | No | Notch filter 3 coefficient Z |
| `Notch coeff z 4` | RW | No | Notch filter 4 coefficient Z |

### Y Axis (~35 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Y Setpoint` | RW | No | Y feedback setpoint |
| `AI Y plot` | READ | No | Current Y sensor reading |
| `pg Y` | RW | No | Proportional gain Y |
| `Upper lim Y` | RW | No | Upper saturation limit Y |
| `Lower lim Y` | RW | No | Lower saturation limit Y |
| ` ig Y` | RW | No | Integral gain Y *(leading space)* |
| `fb Y plot` | READ | No | Feedback output Y |
| `dg Y` | RW | No | Derivative gain Y |
| `dg Y before` | RW | No | Derivative gain Y (before chamber) |
| ` ig Y before` | RW | No | Integral gain Y (before chamber) *(leading space)* |
| `pg Y before` | RW | No | Proportional gain Y (before chamber) |
| `DC offset Y` | RW | No | DC offset added to Y feedback |
| `fb Y before chamber plot` | READ | No | Feedback Y before chamber |
| `tot_fb Y plot` | READ | No | Total feedback Y |
| `Y before Setpoint` | RW | No | Y before-chamber setpoint |
| `AI Y before chamber plot` | READ | No | Y sensor before chamber |
| `Use Y PID before` | RW | Yes | Enable before-chamber PID Y |
| `HP Coeff Y` | RW | No | High-pass filter coefficient Y |
| `HP Coeff Y before` | RW | No | High-pass filter coefficient Y (before) |
| `dg band Y` | RW | No | Derivative bandpass gain Y |
| `dg band Y before` | RW | No | Derivative bandpass gain Y (before) |
| `HP Coeff band Y` | RW | No | HP bandpass coefficient Y |
| `LP Coeff band Y` | RW | No | LP bandpass coefficient Y |
| `LP Coeff band Y before` | RW | No | LP bandpass coefficient Y (before) |
| `HP coeff band Y before` | RW | No | HP bandpass coefficient Y (before) *(lowercase 'c')* |
| `LP Coeff Y` | RW | No | Low-pass filter coefficient Y |
| `LP Coeff Y before` | RW | No | Low-pass filter coefficient Y (before) |
| `final filter coeff Y` | RW | No | Final output filter Y |
| `final filter coeff Y before` | RW | No | Final output filter Y (before) |
| `Lower lim Y before` | RW | No | Lower limit Y (before chamber) |
| `Upper lim Y before` | RW | No | Upper limit Y (before chamber) |
| `dgy mod` | RW | No | Derivative gain Y modulation |
| `activate COMy` | RW | Yes | Activate COM Y output |
| `Reset y accum` | RW | Yes | Reset Y accumulator |
| `Notch coeff y 1` | RW | No | Notch filter 1 coefficient Y |
| `Notch coeff y 2` | RW | No | Notch filter 2 coefficient Y |
| `Notch coeff y 3` | RW | No | Notch filter 3 coefficient Y |
| `Notch coeff y 4` | RW | No | Notch filter 4 coefficient Y |

### X Axis (~35 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `X Setpoint` | RW | No | X feedback setpoint |
| `AI X plot` | READ | No | Current X sensor reading |
| `pg X` | RW | No | Proportional gain X |
| `Upper lim X` | RW | No | Upper saturation limit X |
| `Lower lim X` | RW | No | Lower saturation limit X |
| `ig X` | RW | No | Integral gain X *(no leading space — differs from Y/Z)* |
| `fb X plot` | READ | No | Feedback output X |
| `dg X` | RW | No | Derivative gain X |
| `dg X before` | RW | No | Derivative gain X (before chamber) |
| ` ig X before` | RW | No | Integral gain X (before chamber) *(leading space)* |
| `pg X before` | RW | No | Proportional gain X (before chamber) |
| `DC offset X` | RW | No | DC offset added to X feedback |
| `fb X before chamber plot` | READ | No | Feedback X before chamber |
| `tot_fb X plot` | READ | No | Total feedback X |
| `X before Setpoint` | RW | No | X before-chamber setpoint |
| `AI X before chamber plot` | READ | No | X sensor before chamber |
| `Use X PID before` | RW | Yes | Enable before-chamber PID X |
| `HP Coeff X` | RW | No | High-pass filter coefficient X |
| `HP Coeff X before` | RW | No | High-pass filter coefficient X (before) |
| `dg band X` | RW | No | Derivative bandpass gain X |
| `dg band X before` | RW | No | Derivative bandpass gain X (before) |
| `HP Coeff band X` | RW | No | HP bandpass coefficient X |
| `LP Coeff band X` | RW | No | LP bandpass coefficient X |
| `LP Coeff band X before` | RW | No | LP bandpass coefficient X (before) |
| `HP coeff band X before` | RW | No | HP bandpass coefficient X (before) *(lowercase 'c')* |
| `LP Coeff X` | RW | No | Low-pass filter coefficient X |
| `LP Coeff X before` | RW | No | Low-pass filter coefficient X (before) |
| `final filter coeff X` | RW | No | Final output filter X |
| `final filter coeff X before` | RW | No | Final output filter X (before) |
| `Lower lim X before` | RW | No | Lower limit X (before chamber) |
| `Upper lim X before` | RW | No | Upper limit X (before chamber) |
| `dgx mod` | RW | No | Derivative gain X modulation |
| `activate COMx` | RW | Yes | Activate COM X output |
| `Reset x accum` | RW | Yes | Reset X accumulator |
| `Notch coeff x 1` | RW | No | Notch filter 1 coefficient X |
| `Notch coeff x 2` | RW | No | Notch filter 2 coefficient X |
| `Notch coeff x 3` | RW | No | Notch filter 3 coefficient X |
| `Notch coeff x 4` | RW | No | Notch filter 4 coefficient X |

### Arbitrary Waveform (10 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Arb gain (ch0)` | RW | No | Arb waveform gain channel 0 |
| `Arb gain (ch1)` | RW | No | Arb waveform gain channel 1 |
| `Arb gain (ch2)` | RW | No | Arb waveform gain channel 2 |
| `write_address` | RW | No | Waveform write address |
| `data_buffer_1` | RW | No | Data buffer 1 |
| `data_buffer2` | RW | No | Data buffer 2 *(no underscore)* |
| `data_buffer3` | RW | No | Data buffer 3 *(no underscore)* |
| `Arb steps per cycle` | RW | No | Steps per waveform cycle |
| `ready_to_write` | READ | No | Buffer write ready indicator |
| `written_address` | READ | No | Last written address indicator |

### EOM (8 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `EOM_amplitude` | RW | No | EOM drive amplitude |
| `EOM_threshold` | RW | No | EOM threshold (0 to 1) |
| `EOM reset` | RW | Yes | Reset EOM |
| `EOM_seed` | RW | No | EOM random seed |
| `EOM_offset` | RW | No | EOM DC offset (-10 to 10 V) |
| `eom sine frequency (periods/tick)` | RW | No | EOM sine frequency in FPGA units |
| `Amplitude_sine_EOM` | RW | No | Sine amplitude for EOM |
| `EOM_amplitude_out` | READ | No | EOM amplitude output indicator |

### COM Output (5 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Trigger for COM out` | RW | Yes | Trigger COM output |
| `offset` | RW | No | COM output offset |
| `amplitude` | RW | No | COM output amplitude |
| `frequency (periods/tick)` | RW | No | COM output frequency in FPGA units |
| `duty cycle (periods)` | RW | No | COM output duty cycle |

### Global (6 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Big Number` | READ | No | Counter tick |
| `X_emergency_threshould` | RW | No | X emergency threshold *(note typo in name)* |
| `Y_emergency_threshould` | RW | No | Y emergency threshold *(note typo in name)* |
| `No_integral_gain` | RW | Yes | Disable integral gain globally |
| `master x` | RW | Yes | Master enable X feedback |
| `master y` | RW | Yes | Master enable Y feedback |

### AO Channels 4-7 / Rotation Control (~19 registers)

| Register name | Access | Bool | Description |
|---------------|--------|------|-------------|
| `Reset voltage` | RW | Yes | Reset AO voltage outputs |
| `If revert AO4 and AO5` | RW | Yes | Revert AO4 and AO5 outputs |
| `If scan frequency (AO6 and AO7)?` | RW | Yes | Scan frequency on AO6 and AO7 |
| `frequency AO4` | RW | No | AO4 frequency (FPGA units) |
| `reset AO4` | RW | Yes | Reset AO4 |
| `phase offset AO4` | RW | No | AO4 phase offset |
| `Amplitude AO4` | RW | No | AO4 amplitude |
| `frequency AO5` | RW | No | AO5 frequency (FPGA units) |
| `reset AO5` | RW | Yes | Reset AO5 |
| `phase offset AO5` | RW | No | AO5 phase offset |
| `Amplitude AO5` | RW | No | AO5 amplitude |
| `frequency AO6` | RW | No | AO6 frequency (FPGA units) |
| `reset AO6` | RW | Yes | Reset AO6 |
| `phase offset AO6` | RW | No | AO6 phase offset |
| `Amplitude AO6` | RW | No | AO6 amplitude |
| `frequency AO7` | RW | No | AO7 frequency (FPGA units) |
| `reset AO7` | RW | Yes | Reset AO7 |
| `phase offset AO7` | RW | No | AO7 phase offset |
| `Amplitude AO7` | RW | No | AO7 amplitude |

---

## Lookup Helpers

```python
from fpga_registers import (
    REGISTER_MAP,       # dict[str, RegisterDef] — name → definition
    ALL_NAMES,          # list[str] — all register names in definition order
    DEFAULTS,           # dict[str, float] — all names → 0.0
    names_by_category,  # (Category) → list[str]
    writable_registers, # () → list[RegisterDef]
    readable_registers, # () → list[RegisterDef]
)

# Example usage
reg = REGISTER_MAP["pg Z"]
print(reg.access)  # Access.RW

z_regs = names_by_category(Category.Z_AXIS)
print(len(z_regs))  # ~40

writables = writable_registers()
print(len(writables))  # ~130
```

---

## FPGA Sample Rate

```python
FPGA_SAMPLE_RATE = 100_000  # Hz
```

Derived from the LabVIEW `Count(uSec)` register reading `10`, giving a loop
period of 10 us → 100 kHz sample rate.  This constant is used by all
coefficient conversion functions as the default `sample_rate` parameter.

---

## Host-Side Parameters

Host parameters exist **only in the GUI and session files** — they are never
read from or written to the FPGA directly.  Instead, the GUI uses them as
inputs to `compute_coefficients()`, which produces the actual FPGA register
values.

```python
from fpga_registers import (
    HOST_PARAMS,           # list[HostParam] — all host param definitions
    HOST_PARAM_MAP,        # dict[str, HostParam] — name → definition
    HOST_PARAM_DEFAULTS,   # dict[str, float] — name → default value
    host_params_by_category, # (Category) → list[HostParam]
)
```

---

## Complete Host Parameter Table

### X Axis Filter Parameters (17 params)

| Name | Default | Description |
|------|---------|-------------|
| `hp freq X` | 400.0 | HP cutoff X (Hz) |
| `lp freq X` | 130.0 | LP cutoff X (Hz) |
| `LP FF X` | 1300.0 | Final LP filter X (Hz) |
| `hp freq X before` | 5000.0 | HP cutoff X before (Hz) |
| `lp freq X before` | 5000.0 | LP cutoff X before (Hz) |
| `hp freq bandX` | 5000.0 | HP band freq X (Hz) |
| `lp freq bandX` | 5000.0 | LP band freq X (Hz) |
| `hp freq X band before` | 5000.0 | HP band freq X before (Hz) |
| `lp freq X band before` | 5000.0 | LP band freq X before (Hz) |
| `notch freq 1 x` | 960.0 | Notch 1 center frequency X (Hz) |
| `notch Q 1 x` | 1.0 | Notch 1 quality factor X |
| `notch freq 2 x` | 1309.0 | Notch 2 center frequency X (Hz) |
| `notch Q 2 x` | 2.0 | Notch 2 quality factor X |
| `notch freq 3 x` | 1000.0 | Notch 3 center frequency X (Hz) |
| `notch Q 3 x` | 5.0 | Notch 3 quality factor X |
| `notch freq 4 x` | 1000.0 | Notch 4 center frequency X (Hz) |
| `notch Q 4 x` | 5.0 | Notch 4 quality factor X |

### Y Axis Filter Parameters (17 params)

| Name | Default | Description |
|------|---------|-------------|
| `hp freq Y` | 400.0 | HP cutoff Y (Hz) |
| `lp freq Y` | 130.0 | LP cutoff Y (Hz) |
| `LP FF Y` | 1300.0 | Final LP filter Y (Hz) |
| `hp freq Y before` | 5000.0 | HP cutoff Y before (Hz) |
| `lp freq Y before` | 5000.0 | LP cutoff Y before (Hz) |
| `hp freq bandY` | 5000.0 | HP band freq Y (Hz) |
| `lp freq bandY` | 5000.0 | LP band freq Y (Hz) |
| `hp freq Y band before` | 5000.0 | HP band freq Y before (Hz) |
| `lp freq Y band before` | 5000.0 | LP band freq Y before (Hz) |
| `notch freq 1 y` | 267.0 | Notch 1 center frequency Y (Hz) |
| `notch Q 1 y` | 1.0 | Notch 1 quality factor Y |
| `notch freq 2 y` | 340.0 | Notch 2 center frequency Y (Hz) |
| `notch Q 2 y` | 2.0 | Notch 2 quality factor Y |
| `notch freq 3 y` | 1000.0 | Notch 3 center frequency Y (Hz) |
| `notch Q 3 y` | 5.0 | Notch 3 quality factor Y |
| `notch freq 4 y` | 1000.0 | Notch 4 center frequency Y (Hz) |
| `notch Q 4 y` | 5.0 | Notch 4 quality factor Y |

### Z Axis Filter Parameters (18 params)

| Name | Default | Description |
|------|---------|-------------|
| `hp freq Z` | 4000.0 | HP cutoff Z (Hz) |
| `lp freq Z` | 4000.0 | LP cutoff Z (Hz) |
| `LP FF Z` | 4200.0 | Final LP filter Z (Hz) |
| `LP FF Z before` | 5000.0 | Final LP filter Z before (Hz) |
| `hp freq Z before` | 5000.0 | HP cutoff Z before (Hz) |
| `lp freq Z before` | 5000.0 | LP cutoff Z before (Hz) |
| `hp freq bandZ` | 5000.0 | HP band freq Z (Hz) |
| `lp freq bandZ` | 49.0 | LP band freq Z (Hz) |
| `hp freq band Z before` | 5000.0 | HP band freq Z before (Hz) |
| `lp freq band Z before` | 5000.0 | LP band freq Z before (Hz) |
| `notch freq 1 z` | 960.0 | Notch 1 center frequency Z (Hz) |
| `notch Q 1 z` | 4.0 | Notch 1 quality factor Z |
| `notch freq 2 z` | 1309.0 | Notch 2 center frequency Z (Hz) |
| `notch Q 2 z` | 4.0 | Notch 2 quality factor Z |
| `notch freq 3 z` | 1000.0 | Notch 3 center frequency Z (Hz) |
| `notch Q 3 z` | 5.0 | Notch 3 quality factor Z |
| `notch freq 4 z` | 1000.0 | Notch 4 center frequency Z (Hz) |
| `notch Q 4 z` | 5.0 | Notch 4 quality factor Z |

### Z Ramp Power Parameters (3 params)

| Name | Default | Description |
|------|---------|-------------|
| `End value power` | 3000.0 | Power ramp target value |
| `Step power` | 0.0 | Power ramp step size |
| `Delay Time (s) power` | 0.05 | Power ramp delay (s) |

### Arb Waveform Parameters (6 params)

| Name | Default | Description |
|------|---------|-------------|
| `End value arb (ch0)` | 0.0 | Arb ramp target channel 0 |
| `End value arb (ch1)` | 0.1 | Arb ramp target channel 1 |
| `Step arb (ch0)` | 0.0 | Arb ramp step channel 0 |
| `Step arb (ch1)` | 0.0 | Arb ramp step channel 1 |
| `Delay Time (s) arb` | 0.001 | Arb ramp delay (s) |
| `z arb scale` | 0.0 | Z arb waveform scale |

### EOM (1 param)

| Name | Default | Description |
|------|---------|-------------|
| `Frequency_sine_EOM (Hz)` | 0.0 | EOM sine frequency in Hz |

### COM Output (1 param)

| Name | Default | Description |
|------|---------|-------------|
| `frequency (kHz)` | 0.0015 | COM output frequency in kHz |

### AO Channels (4 params)

| Name | Default | Description |
|------|---------|-------------|
| `frequency AO4 (Hz)` | 0.0 | AO4 frequency in Hz |
| `frequency AO5 (Hz)` | 7.0 | AO5 frequency in Hz |
| `frequency AO6 (Hz)` | 80.0 | AO6 frequency in Hz |
| `frequency AO7 (Hz)` | 80.0 | AO7 frequency in Hz |

---

## Coefficient Conversion Functions

All functions take an optional `sample_rate` parameter (default: `FPGA_SAMPLE_RATE = 100,000 Hz`).  They return `0.0` for invalid inputs (negative frequency, frequency above Nyquist, etc.).

### `freq_to_lp_coeff(freq_hz, sample_rate=100000) → float`

First-order IIR low-pass coefficient:

$$\alpha = e^{-2\pi f / f_s}$$

- Input: cutoff frequency in Hz
- Output: coefficient in range (0, 1)
- Returns `0.0` if `freq_hz <= 0` or `freq_hz >= fs/2`

### `freq_to_hp_coeff(freq_hz, sample_rate=100000) → float`

First-order IIR high-pass coefficient (same formula as LP — the FPGA
applies it differently):

$$\alpha = e^{-2\pi f / f_s}$$

### `freq_q_to_notch_coeff(freq_hz, q, sample_rate=100000) → float`

Notch filter coefficient from center frequency and quality factor:

$$r = \max\!\left(0,\; 1 - \frac{\pi \cdot f_0 / Q}{f_s}\right)$$

$$\text{coeff} = 2\,r\,\cos\!\left(\frac{2\pi f_0}{f_s}\right)$$

- Input: center frequency + Q factor
- Output: single coefficient (the FPGA's notch implementation uses this value)
- Returns `0.0` if `freq_hz <= 0` or `q <= 0`

### `hz_to_periods_per_tick(freq_hz, sample_rate=100000) → float`

Convert a frequency in Hz to the FPGA's "periods per tick" representation:

$$\text{periods\_per\_tick} = \frac{f_s}{f}$$

Used for EOM sine frequency and COM output frequency registers.

---

## compute_coefficients()

The master conversion function.  Given an axis and a dict of host-side
frequency/Q parameters, computes all FPGA filter-coefficient register
values for that axis.

```python
from fpga_registers import compute_coefficients

coeffs = compute_coefficients("Z", {
    "hp freq Z": 4000.0,
    "lp freq Z": 4000.0,
    "LP FF Z": 4200.0,
    "LP FF Z before": 5000.0,
    "hp freq Z before": 5000.0,
    "lp freq Z before": 5000.0,
    "hp freq bandZ": 5000.0,
    "lp freq bandZ": 49.0,
    "hp freq band Z before": 5000.0,
    "lp freq band Z before": 5000.0,
    "notch freq 1 z": 960.0,
    "notch Q 1 z": 4.0,
    "notch freq 2 z": 1309.0,
    "notch Q 2 z": 4.0,
    "notch freq 3 z": 1000.0,
    "notch Q 3 z": 5.0,
    "notch freq 4 z": 1000.0,
    "notch Q 4 z": 5.0,
})
```

### Output registers computed

For a given axis `{A}` (uppercase) and `{a}` (lowercase):

| Register written | Source host param(s) | Formula |
|------------------|---------------------|---------|
| `HP Coeff {A}` | `hp freq {A}` | `freq_to_hp_coeff()` |
| `LP Coeff {A}` | `lp freq {A}` | `freq_to_lp_coeff()` |
| `HP Coeff {A} before` | `hp freq {A} before` | `freq_to_hp_coeff()` |
| `LP Coeff {A} before` | `lp freq {A} before` | `freq_to_lp_coeff()` |
| `HP Coeff band {A}` | `hp freq band{A}` | `freq_to_hp_coeff()` |
| `LP Coeff band {A}` | `lp freq band{A}` | `freq_to_lp_coeff()` |
| `HP coeff band {A} before` | `hp freq {A} band before` | `freq_to_hp_coeff()` |
| `LP Coeff band {A} before` | `lp freq {A} band before` | `freq_to_lp_coeff()` |
| `final filter coeff {A}` | `LP FF {A}` | `freq_to_lp_coeff()` |
| `final filter coeff Z before` | `LP FF Z before` | `freq_to_lp_coeff()` *(Z only)* |
| `Notch coeff {a} 1..4` | `notch freq {n} {a}`, `notch Q {n} {a}` | `freq_q_to_notch_coeff()` |

### Host-param name quirks

The band-before-chamber host-param names differ slightly between axes:
- Z axis uses: `"hp freq band Z before"` (space between "band" and axis)
- X/Y axes use: `"hp freq X band before"` (axis between "freq" and "band")
- `compute_coefficients()` handles both patterns with fallback lookups

---

## Per-Axis Register Pattern

Each axis (X, Y, Z) follows the same register structure.  Replace `{A}`
with the uppercase letter and `{a}` with lowercase:

| Pattern | Purpose |
|---------|---------|
| `{A} Setpoint` | Feedback setpoint |
| `AI {A} plot` | Current sensor reading (READ) |
| `pg {A}` / ` ig {A}` / `dg {A}` | PID gains |
| `pg {A} before` / ` ig {A} before` / `dg {A} before` | Before-chamber PID |
| `dg band {A}` / `dg band {A} before` | Derivative bandpass gains |
| `Upper lim {A}` / `Lower lim {A}` | Saturation limits |
| `Upper lim {A} before` / `Lower lim {A} before` | Before-chamber limits |
| `DC offset {A}` | DC offset added to feedback |
| `HP Coeff {A}` / `LP Coeff {A}` | HP/LP filter coefficients |
| `HP Coeff band {A}` / `LP Coeff band {A}` | Bandpass filter coefficients |
| `final filter coeff {A}` | Final output low-pass filter |
| `Notch coeff {a} 1..4` | Notch filter coefficients |
| `fb {A} plot` / `tot_fb {A} plot` | Feedback outputs (READ) |
| `Use {A} PID before` | Enable before-chamber PID (bool) |
| `activate COM{a}` | Activate COM output for axis (bool) |
| `Reset {a} accum` | Reset accumulator (bool) |
| `dg{a} mod` / `pg {A} mod` | Gain modulation |

> **Naming hazard:** The integral gain register name has a leading space for
> Y and Z (`" ig Y"`, `" ig Z"`) but no leading space for X (`"ig X"`).
> This is an artifact of the original LabVIEW bitfile.  The GUI code handles
> this discrepancy explicitly.
