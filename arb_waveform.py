"""
arb_waveform.py

Pure-numpy arbitrary waveform generators for the FPGA bead-drive buffer.

All generators return a WaveformResult whose .samples array is normalized
to [-1, +1] and ready to write to the FPGA arb buffer file.

FPGA context
------------
The FPGA arb buffer holds *n_points* samples that the hardware clocks out
at a fixed rate (the FPGA arb sample rate, Sa/s).  The perceived drive
frequency is:

    f_drive = (fpga_sample_rate / n_points) × n_cycles

For single-tone shapes (sine, triangle, trapezoid) the user supplies
*n_cycles* (periods per buffer), which sets the frequency at playback.

For the frequency comb the tones are given in absolute Hz, so the
sample_rate is required to build the correct time axis.

File format (for load_arb_waveform)
------------------------------------
FPGA buffers expect a whitespace- or comma-separated text file with one
sample per line and up to 3 columns (data_buffer_0, data_buffer_1,
data_buffer_2).  save_waveform() writes the same waveform on all 3
columns by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class WaveformResult:
    samples: np.ndarray   # float64, normalized to [-1, +1]
    rms: float            # RMS of normalized signal
    peak: float           # always 1.0 after normalization
    crest_factor: float   # peak / rms  (sine = √2 ≈ 1.414)
    n_points: int
    description: str


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _finalize(samples: np.ndarray, description: str) -> WaveformResult:
    """Normalize *samples* to ±1 and compute statistics."""
    samples = np.asarray(samples, dtype=np.float64)
    peak = float(np.max(np.abs(samples)))
    if peak > 0.0:
        samples = samples / peak
    else:
        samples = np.zeros_like(samples)
    rms = float(np.sqrt(np.mean(samples ** 2)))
    crest = (1.0 / rms) if rms > 0.0 else float("inf")
    return WaveformResult(
        samples=samples,
        rms=rms,
        peak=1.0,
        crest_factor=crest,
        n_points=len(samples),
        description=description,
    )


# ---------------------------------------------------------------------------
# Sine
# ---------------------------------------------------------------------------

def generate_sine(n_points: int,
                  n_cycles: float = 1.0,
                  phase_deg: float = 0.0) -> WaveformResult:
    """
    Single-tone sine wave.

    Parameters
    ----------
    n_points  : buffer length in samples
    n_cycles  : number of complete periods packed into the buffer
    phase_deg : start phase in degrees
    """
    t = np.linspace(0.0, 2.0 * np.pi * n_cycles, n_points, endpoint=False)
    samples = np.sin(t + np.deg2rad(phase_deg))
    return _finalize(
        samples,
        f"Sine  {n_cycles:.3g} cycles  φ={phase_deg:.1f}°  {n_points} pts",
    )


# ---------------------------------------------------------------------------
# Triangle / ramp
# ---------------------------------------------------------------------------

def generate_triangle(n_points: int,
                      n_cycles: float = 1.0,
                      symmetry: float = 0.5) -> WaveformResult:
    """
    Triangle / ramp wave.

    Parameters
    ----------
    n_points  : buffer length in samples
    n_cycles  : periods per buffer
    symmetry  : fraction of the period spent rising
                0.0 → falling sawtooth
                0.5 → symmetric triangle  (default)
                1.0 → rising sawtooth
    """
    symmetry = float(np.clip(symmetry, 0.0, 1.0))
    spc   = n_points / n_cycles                         # samples per cycle
    phase = (np.arange(n_points) % spc) / spc           # 0..1 within cycle

    if symmetry == 0.0:
        samples = 2.0 * phase - 1.0
    elif symmetry == 1.0:
        samples = 1.0 - 2.0 * phase
    else:
        rising  = phase < symmetry
        samples = np.where(
            rising,
            -1.0 + 2.0 * phase / symmetry,
            1.0  - 2.0 * (phase - symmetry) / (1.0 - symmetry),
        )

    sym_label = {0.0: "rising saw", 0.5: "triangle", 1.0: "falling saw"}.get(
        symmetry, f"sym={symmetry:.2f}")
    return _finalize(
        samples,
        f"Triangle  {sym_label}  {n_cycles:.3g} cycles  {n_points} pts",
    )


# ---------------------------------------------------------------------------
# Trapezoid
# ---------------------------------------------------------------------------

def generate_trapezoid(n_points: int,
                       n_cycles: float = 1.0,
                       rise_frac: float = 0.1,
                       high_frac: float = 0.4,
                       fall_frac: float = 0.1) -> WaveformResult:
    """
    Trapezoidal waveform.  Fractions of one period (must sum to ≤ 1):

      rise_frac  ramp from -1 → +1
      high_frac  hold at +1
      fall_frac  ramp from +1 → -1
      low_frac   hold at -1  (1 − rise − high − fall)

    Parameters
    ----------
    n_points  : buffer length in samples
    n_cycles  : periods per buffer
    rise_frac : rise time fraction  (0..1)
    high_frac : high-hold fraction  (0..1)
    fall_frac : fall time fraction  (0..1)
    """
    low_frac = 1.0 - rise_frac - high_frac - fall_frac
    if low_frac < -1e-9:
        raise ValueError(
            f"rise+high+fall = {rise_frac+high_frac+fall_frac:.4f} > 1"
        )
    low_frac = max(low_frac, 0.0)

    p1 = rise_frac
    p2 = p1 + high_frac
    p3 = p2 + fall_frac

    spc   = n_points / n_cycles
    phase = (np.arange(n_points) % spc) / spc

    samples = np.full(n_points, -1.0, dtype=np.float64)

    if p1 > 0:
        m = phase < p1
        samples[m] = -1.0 + 2.0 * phase[m] / p1

    m = (phase >= p1) & (phase < p2)
    samples[m] = 1.0

    if p3 > p2:
        m = (phase >= p2) & (phase < p3)
        samples[m] = 1.0 - 2.0 * (phase[m] - p2) / (p3 - p2)

    # phase >= p3: already -1.0

    return _finalize(
        samples,
        f"Trapezoid  rise={rise_frac:.2f} high={high_frac:.2f} "
        f"fall={fall_frac:.2f} low={low_frac:.2f}  "
        f"{n_cycles:.3g} cycles  {n_points} pts",
    )


# ---------------------------------------------------------------------------
# Frequency comb with Monte Carlo phase optimisation
# ---------------------------------------------------------------------------

def generate_comb(
    n_points: int,
    sample_rate: float,
    frequencies: List[float],
    n_trials: int = 1000,
    progress_cb: Optional[Callable[[int, int, float], None]] = None,
    stop_flag: Optional[List[bool]] = None,
) -> WaveformResult:
    """
    Frequency comb via Monte Carlo phase optimisation.

    Generates *n_trials* random phase assignments for *frequencies*,
    returns the waveform (normalized to ±1) whose RMS is maximum —
    i.e., the combination that most efficiently fills the DAC range
    (minimum crest factor = peak/RMS).

    The vectorised inner loop uses matrix multiplication so it scales
    well to large trial counts and many tones.

    Parameters
    ----------
    n_points    : buffer length in samples
    sample_rate : FPGA arb sample rate in Sa/s (defines the time axis)
    frequencies : tone frequencies in Hz
    n_trials    : Monte Carlo draws
    progress_cb : optional callback(trial_idx, total, current_best_rms)
                  called every 50 trials and on completion
    stop_flag   : mutable list[bool]; set stop_flag[0] = True from
                  another thread to interrupt the optimisation early

    Returns
    -------
    WaveformResult normalized to ±1 with the best phases found.
    """
    if not frequencies:
        raise ValueError("frequencies list is empty")
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be > 0")

    freqs = np.asarray(frequencies, dtype=np.float64)
    t     = np.arange(n_points, dtype=np.float64) / sample_rate

    # Pre-compute carrier matrices — shape (n_freqs, n_points)
    # sin(2π f t + φ) = sin(2π f t)cos(φ) + cos(2π f t)sin(φ)
    sin_carriers = np.sin(2.0 * np.pi * freqs[:, None] * t[None, :])
    cos_carriers = np.cos(2.0 * np.pi * freqs[:, None] * t[None, :])

    best_rms     = -1.0
    best_samples: Optional[np.ndarray] = None
    rng          = np.random.default_rng()

    for i in range(n_trials):
        if stop_flag is not None and stop_flag[0]:
            break

        phases = rng.uniform(0.0, 2.0 * np.pi, len(freqs))
        # Σ_k [sin(2π f_k t) cos(φ_k) + cos(2π f_k t) sin(φ_k)]
        signal = (np.cos(phases) @ sin_carriers
                  + np.sin(phases) @ cos_carriers)

        peak = np.max(np.abs(signal))
        if peak == 0.0:
            continue

        rms_norm = float(np.sqrt(np.mean((signal / peak) ** 2)))
        if rms_norm > best_rms:
            best_rms     = rms_norm
            best_samples = (signal / peak).copy()

        if progress_cb is not None and i % 50 == 0:
            progress_cb(i, n_trials, best_rms)

    if progress_cb is not None:
        progress_cb(n_trials, n_trials, best_rms)

    if best_samples is None:
        best_samples = np.zeros(n_points)

    n_f      = len(frequencies)
    freq_str = (
        ", ".join(f"{f:.0f}" for f in frequencies)
        if n_f <= 5
        else f"{n_f} tones"
    )
    stopped = stop_flag is not None and stop_flag[0]
    return _finalize(
        best_samples,
        f"Comb  [{freq_str}] Hz  "
        f"{'stopped' if stopped else f'MC={n_trials}'}  "
        f"RMS/peak={best_rms:.4f}  {n_points} pts",
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_waveform(result: WaveformResult, path: str | Path,
                  n_channels: int = 3) -> None:
    """
    Save *result* to a whitespace-separated text file.

    The file has *n_channels* identical columns (FPGA expects up to 3:
    data_buffer_0, data_buffer_1, data_buffer_2).

    Parameters
    ----------
    result     : WaveformResult to save
    path       : output file path (.txt / .csv / .dat)
    n_channels : number of identical columns to write (default 3)
    """
    data = result.samples[:, None]                # (n, 1)
    data = np.tile(data, (1, n_channels))         # (n, n_channels)
    header = (
        f"arb_waveform  pts={result.n_points}  "
        f"rms={result.rms:.6f}  crest={result.crest_factor:.4f}\n"
        f"{result.description}"
    )
    np.savetxt(str(path), data, fmt="%.8f",
               delimiter="  ", header=header, comments="# ")


def load_waveform_file(path: str | Path) -> np.ndarray:
    """
    Load a waveform file (up to 3 columns) and return the first column
    as a float64 array.
    """
    raw = np.loadtxt(str(path), comments="#")
    if raw.ndim > 1:
        raw = raw[:, 0]
    peak = np.max(np.abs(raw))
    return raw / peak if peak > 0 else raw
