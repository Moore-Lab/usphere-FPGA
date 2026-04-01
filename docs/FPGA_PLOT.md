# fpga_plot.py — Plotting Widget Reference

`fpga_plot.py` provides a dual-tab real-time plotting widget that mirrors
the LabVIEW front panel's **Bead** (time-domain) and **Plot 0**
(frequency-domain PSD) views.  It can be embedded in the GUI or run
standalone.

---

## Table of Contents

1. [Overview](#overview)
2. [PLOT_CHANNELS](#plot_channels)
3. [FPGAPlotWidget](#fpgaplotwidget)
   - [Constructor](#constructor)
   - [Public API](#public-api)
   - [Bead Tab (Time Domain)](#bead-tab-time-domain)
   - [Plot 0 Tab (PSD / FFT)](#plot-0-tab-psd--fft)
   - [Channel Selection](#channel-selection)
   - [Window Functions](#window-functions)
   - [History Length](#history-length)
4. [PSD Computation Details](#psd-computation-details)
5. [Standalone Mode](#standalone-mode)

---

## Overview

The widget consists of:

- **Left panel** — channel checkboxes, history length spinner, window function
  selector
- **Right panel** — two matplotlib tabs:
  - **Bead** — scrolling time-domain strip chart of selected channels
  - **Plot 0** — log-log power spectral density of the same channels

Both tabs share the same channel selection and update simultaneously on
each call to `push_values()`.

---

## PLOT_CHANNELS

A module-level list of 17 dicts defining which FPGA indicators can be plotted:

| Channel name | Label | Color | Description |
|-------------|-------|-------|-------------|
| `AI Z plot` | Z sensor | #1f77b4 | Z-axis sensor reading |
| `AI Z before chamber plot` | Z before chamber | #aec7e8 | Z sensor before chamber |
| `fb Z plot` | Z feedback | #ff7f0e | Z-axis feedback output |
| `fb Z before chamber plot` | Z fb before | #ffbb78 | Z feedback before chamber |
| `tot_fb Z plot` | Z total feedback | #e6550d | Z total feedback |
| `AI Y plot` | Y sensor | #2ca02c | Y-axis sensor reading |
| `AI Y before chamber plot` | Y before chamber | #98df8a | Y sensor before chamber |
| `fb Y plot` | Y feedback | #d62728 | Y-axis feedback output |
| `fb Y before chamber plot` | Y fb before | #ff9896 | Y feedback before chamber |
| `tot_fb Y plot` | Y total feedback | #a63603 | Y total feedback |
| `AI X plot` | X sensor | #9467bd | X-axis sensor reading |
| `AI X before chamber plot` | X before chamber | #c5b0d5 | X sensor before chamber |
| `fb X plot` | X feedback | #8c564b | X-axis feedback output |
| `fb X before chamber plot` | X fb before | #c49c94 | X feedback before chamber |
| `tot_fb X plot` | X total feedback | #7b4173 | X total feedback |
| `accum out z1` | Accum Z1 | #17becf | Accumulator z1 output |
| `accum out z2` | Accum Z2 | #bcbd22 | Accumulator z2 output |

Default: the first 3 channels are checked (Z sensor, Z before chamber, Z feedback).

---

## FPGAPlotWidget

### Constructor

```python
FPGAPlotWidget(parent=None, max_points=500, sample_rate=5.0)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `parent` | `QWidget` | `None` | Parent widget |
| `max_points` | `int` | `500` | Ring-buffer length (number of poll samples stored) |
| `sample_rate` | `float` | `5.0` | Approximate samples/sec (= 1000 / poll_interval_ms); used for PSD frequency axis fallback |

### Internal state

| Variable | Type | Description |
|----------|------|-------------|
| `_buffers` | `dict[str, deque]` | Ring buffers: channel name → deque of `(timestamp, value)` tuples |
| `_checkboxes` | `dict[str, QCheckBox]` | Channel name → checkbox widget |
| `_t0` | `float` | `time.monotonic()` reference for relative timestamps |
| `_max_points` | `int` | Current ring-buffer max length |
| `_sample_rate` | `float` | Fallback sample rate (overridden by actual timestamp estimation) |

---

### Public API

| Method | Description |
|--------|-------------|
| `push_values(values: dict[str, float])` | Append a new sample for each tracked channel. Called by the GUI on every monitor poll cycle. Triggers a full replot. |
| `clear()` | Clear all history buffers and reset the time origin. |
| `set_sample_rate(rate: float)` | Update the fallback sample rate (for PSD when timestamps aren't reliable). |

---

### Bead Tab (Time Domain)

A scrolling strip chart showing signal values over time.

- **X-axis:** Time in seconds (relative to `_t0`, i.e. seconds since widget creation)
- **Y-axis:** Register value (raw FPGA units)
- **Lines:** One trace per active (checked) channel, using the channel's assigned color
- **Legend:** Upper-left corner, 7pt font, only shown when channels are active
- **Grid:** Enabled, alpha 0.3

The plot auto-scales to show all data in the ring buffer.

---

### Plot 0 Tab (PSD / FFT)

A log-log power spectral density plot.

- **X-axis:** Frequency in Hz (log scale), computed from actual timestamp spacing
- **Y-axis:** PSD (log scale), units: V²/Hz (or equivalent raw² / Hz)
- **Lines:** One PSD curve per active channel
- **Legend:** Upper-right corner, 7pt font
- **Grid:** Both major and minor, alpha 0.3
- **DC component:** Excluded (plot starts at the first non-zero frequency bin)
- **Minimum data:** Requires at least 8 samples per channel to compute PSD

---

### Channel Selection

The left panel shows 17 checkboxes (one per `PLOT_CHANNELS` entry).
Checking/unchecking a channel immediately triggers a replot of both tabs.

Default state: first 3 channels checked.

The `_active_channels()` helper returns only channels that are:
1. Checkbox is checked, AND
2. Buffer contains at least 2 data points

---

### Window Functions

A `QComboBox` in the left panel with 4 options:

| Window | numpy function | Description |
|--------|---------------|-------------|
| hanning | `np.hanning(n)` | Default; good general-purpose window |
| hamming | `np.hamming(n)` | Slightly different sidelobe profile |
| blackman | `np.blackman(n)` | Better sidelobe suppression, wider main lobe |
| boxcar | `np.ones(n)` | No windowing (rectangular) |

Changing the window triggers a replot.

---

### History Length

A `QSpinBox` (50–10,000 pts, suffix " pts") controls the ring-buffer
max length.  Changing it resizes all buffers, preserving existing data
(truncating from the front if the new length is shorter).

More points → better frequency resolution in the PSD, but more memory
and slightly slower redraws.

---

## PSD Computation Details

For each active channel with ≥ 8 data points:

1. **Sample rate estimation:** Computed from actual timestamps:
   $$f_s = \frac{n - 1}{t_{n-1} - t_0}$$
   Falls back to `_sample_rate` if `dt ≤ 0`.

2. **Mean subtraction:** `vs = vs - mean(vs)` to remove DC component.

3. **Windowing:** Multiply by the selected window function.

4. **FFT:** `np.fft.rfft(windowed)` — real-input FFT returning only positive frequencies.

5. **PSD normalization:** One-sided power spectral density:
   $$P(f_k) = \frac{2}{f_s \cdot \sum w_i^2} \cdot |X(f_k)|^2$$
   where $w_i$ are window values and $X(f_k)$ are FFT coefficients.

6. **Frequency axis:** `np.fft.rfftfreq(n, d=1/fs)`.

7. **Display:** Skip DC bin (`freqs[1:]`, `psd[1:]`), plot on log-log axes.

---

## Standalone Mode

The widget can be run independently for testing:

```bash
python fpga_plot.py
```

This opens a `QMainWindow` titled "FPGA Plot — Standalone" (1000 x 500 px)
with an empty `FPGAPlotWidget`.  No data is generated — you'd need to call
`push_values()` manually to populate it.
