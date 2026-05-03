"""
test_arb_record.py

Standalone arb waveform diagnostic.
Connects to the FPGA, records fb_X / tot_fb_X before and after
writing each test waveform via Python, then prints stats + saves CSVs.

Usage:
    python test_arb_record.py
"""

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fpga.core import FPGAController, FPGAConfig

RECORD_CHANNELS = ["AI X plot", "fb X plot", "tot_fb X plot"]
RECORD_SECONDS  = 8      # how long to record each phase
POLL_S          = 0.010  # ~100 Hz (Windows will give ~67 Hz in practice)
WAVEFORMS       = [
    "waveforms/43hz_100.csv",
    "waveforms/36hz_100.csv",
]
OUT_DIR = Path("recordings")
OUT_DIR.mkdir(exist_ok=True)


def record(ctrl, label: str, seconds: float) -> list[list]:
    """Poll FPGA registers for `seconds` and return rows."""
    rows = []
    t0 = time.monotonic()
    print(f"  Recording {label!r} for {seconds}s ...", end="", flush=True)
    while time.monotonic() - t0 < seconds:
        vals = ctrl.read_registers(RECORD_CHANNELS)
        t = time.monotonic() - t0
        rows.append([round(t, 6)] + [round(vals.get(c, 0.0), 4) for c in RECORD_CHANNELS])
        time.sleep(POLL_S)
    print(f" {len(rows)} samples")
    return rows


def save_csv(rows: list[list], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s"] + [c.replace(" plot", "").replace(" ", "_")
                               for c in RECORD_CHANNELS])
        w.writerows(rows)
    print(f"  Saved -> {path}")


def stats(rows: list[list], col: int) -> dict:
    import statistics
    vals = [r[col] for r in rows]
    return {
        "n":    len(vals),
        "mean": round(statistics.mean(vals), 3),
        "rms":  round((sum(v**2 for v in vals) / len(vals)) ** 0.5, 3),
        "pp":   round(max(vals) - min(vals), 3),
    }


def print_stats(rows: list[list], label: str) -> None:
    print(f"\n  === {label} ===")
    for ci, name in enumerate(RECORD_CHANNELS, start=1):
        s = stats(rows, ci)
        print(f"    {name:<20}  n={s['n']}  mean={s['mean']:>10.3f}  "
              f"rms={s['rms']:>10.3f}  p-p={s['pp']:>10.3f}")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg  = FPGAConfig()
    ctrl = FPGAController(cfg)

    print("Connecting to FPGA ...")
    ctrl.connect()
    if not ctrl.is_connected:
        print("ERROR: could not connect")
        sys.exit(1)

    sim = ctrl.is_simulated
    print(f"Connected  (simulated={sim})")

    # Read current arb gain (ch0) for reference
    try:
        gain = ctrl.read_register("Arb gain (ch0)")
        print(f"Arb gain (ch0) = {gain}")
    except Exception as e:
        print(f"Could not read Arb gain: {e}")

    # ── Phase 0: baseline (LabVIEW waveform already loaded) ──────────
    print("\n[Phase 0] Baseline — waveform currently in FPGA buffer")
    rows0 = record(ctrl, "baseline", RECORD_SECONDS)
    save_csv(rows0, OUT_DIR / "phase0_baseline.csv")
    print_stats(rows0, "Baseline (LabVIEW loaded)")

    # ── Phases 1…N: load each waveform via Python ─────────────────────
    for i, wf_path in enumerate(WAVEFORMS, start=1):
        print(f"\n[Phase {i}] Writing {wf_path} via Python ...")
        t_write = time.monotonic()
        ctrl.load_arb_waveform(wf_path)
        elapsed = time.monotonic() - t_write
        print(f"  Write complete in {elapsed:.2f}s")

        tag = Path(wf_path).stem
        rows = record(ctrl, tag, RECORD_SECONDS)
        save_csv(rows, OUT_DIR / f"phase{i}_{tag}.csv")
        print_stats(rows, f"After Python write: {tag}")

    # ── Reload 43hz to leave in a known state ─────────────────────────
    print("\n[Cleanup] Reloading 43hz_100 to restore known state ...")
    ctrl.load_arb_waveform("waveforms/43hz_100.csv")

    ctrl.disconnect()
    print("\nDone.  CSVs saved to recordings/")


if __name__ == "__main__":
    main()
