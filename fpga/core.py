"""
fpga_core.py

Backend for NI PXIe-7856R FPGA control.
Manages the FPGA session, reads/writes registers, and provides a polling
monitor for real-time indicator updates.

Unlike the DAQ plugin (daq_fpga.py) which opens a transient session to
snapshot register values once per file, this module keeps the session
open persistently so that parameters can be tuned interactively.

Simulation mode
---------------
When nifpga is not installed the module operates with an in-memory
register dictionary so the GUI can be developed and tested without
hardware.
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fpga.registers import (
    ALL_NAMES,
    Access,
    DEFAULTS,
    HOST_PARAM_DEFAULTS,
    REGISTER_MAP,
    REGISTERS,
    Category,
    RegisterDef,
    compute_coefficients,
    names_by_category,
    readable_registers,
    writable_registers,
)

try:
    import nifpga
    NIFPGA_AVAILABLE = True
except ImportError:
    NIFPGA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Session log (mirrors DAQ session-log pattern)
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).parent / "fpga_session_log.jsonl"


def _append_log(action: str, data: dict) -> None:
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "action": action,
        **data,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_last_session() -> dict | None:
    """Return the last session config from the log, or None."""
    if not LOG_FILE.exists():
        return None
    try:
        lines = [l for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FPGA connection configuration
# ---------------------------------------------------------------------------

@dataclass
class FPGAConfig:
    bitfile: str = (
        r"C:\Users\yalem\GitHub\Documents\Optlev\LabView Code"
        r"\FPGA code\FPGA Bitfiles"
        r"\Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx"
    )
    resource: str = "PXI1Slot2"
    poll_interval_ms: int = 200     # how often the monitor reads all registers
    plot_interval_ms: int = 10      # fast poll rate for plot indicators (ms)

    def to_dict(self) -> dict:
        return {
            "bitfile": self.bitfile,
            "resource": self.resource,
            "poll_interval_ms": self.poll_interval_ms,
            "plot_interval_ms": self.plot_interval_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FPGAConfig:
        valid = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in valid})


# ---------------------------------------------------------------------------
# FPGA session controller
# ---------------------------------------------------------------------------

class FPGAController:
    """
    Persistent FPGA session with read/write and a polling monitor.

    Callbacks
    ---------
    on_status(msg: str)
        Log / status messages.
    on_registers_updated(values: dict[str, float])
        Called each poll cycle with current register values.
    on_connected()
        Called after a successful connect().
    on_disconnected()
        Called after disconnect().
    """

    def __init__(
        self,
        config: FPGAConfig | None = None,
        on_status=None,
        on_registers_updated=None,
        on_plot_data=None,
        on_connected=None,
        on_disconnected=None,
    ):
        self.config = config or FPGAConfig()
        self._on_status = on_status or print
        self._on_registers_updated = on_registers_updated
        self._on_plot_data = on_plot_data
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._session = None          # nifpga.Session or None
        self._sim_regs: dict[str, float] = dict(DEFAULTS)  # simulation store
        self._connected = False
        self._plot_names: list[str] = []
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_simulated(self) -> bool:
        return not NIFPGA_AVAILABLE

    def connect(self) -> None:
        """Open the FPGA session (or enter simulation mode)."""
        if self._connected:
            return

        cfg = self.config
        ts = datetime.datetime.now().strftime("%H:%M:%S")

        if not NIFPGA_AVAILABLE:
            self._log(f"[{ts}] [SIM] nifpga not available — running in simulation mode")
            self._connected = True
        else:
            self._log(f"[{ts}] Connecting to {cfg.resource} ...")
            self._session = nifpga.Session(
                bitfile=cfg.bitfile,
                resource=cfg.resource,
                run=False,
                reset_if_last_session_on_exit=False,
            )
            self._connected = True
            self._log(f"[{ts}] Connected to {cfg.resource}")

        _append_log("connect", cfg.to_dict())

        if self._on_connected:
            self._on_connected()

    def disconnect(self) -> None:
        """Close the FPGA session and stop the monitor."""
        self.stop_monitor()
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            self._connected = False
        self._log("Disconnected.")
        if self._on_disconnected:
            self._on_disconnected()

    def read_register(self, name: str) -> float:
        """Read a single register. Returns 0.0 if unreadable."""
        with self._lock:
            return self._read_one(name)

    def write_register(self, name: str, value: float) -> None:
        """Write a single register value."""
        reg = REGISTER_MAP.get(name)
        if reg is None:
            raise KeyError(f"Unknown register: {name!r}")
        if reg.access == Access.READ:
            raise ValueError(f"Register {name!r} is read-only")

        with self._lock:
            self._write_one(name, value, reg)

        _append_log("write", {"register": name, "value": value})

    def read_all(self) -> dict[str, float]:
        """Read every register. Returns dict[name, value]."""
        with self._lock:
            return {name: self._read_one(name) for name in ALL_NAMES}

    def read_registers(self, names: list[str]) -> dict[str, float]:
        """Read a subset of registers by name."""
        with self._lock:
            return {name: self._read_one(name) for name in names}

    def write_many(self, values: dict[str, float]) -> dict[str, str]:
        """
        Write multiple registers.  Returns {name: error_msg} for failures
        (empty dict on full success).
        """
        errors: dict[str, str] = {}
        with self._lock:
            for name, value in values.items():
                reg = REGISTER_MAP.get(name)
                if reg is None:
                    errors[name] = "unknown register"
                    continue
                if reg.access == Access.READ:
                    errors[name] = "read-only"
                    continue
                try:
                    self._write_one(name, value, reg)
                except Exception as exc:
                    errors[name] = str(exc)

        if values:
            _append_log("write_many", {"values": values, "errors": errors})
        return errors

    def snapshot(self) -> dict:
        """Return all register values plus connection metadata."""
        vals = self.read_all()
        return {
            "timestamp": datetime.datetime.now().isoformat(),
            "config": self.config.to_dict(),
            "simulated": self.is_simulated,
            "registers": vals,
        }

    def save_snapshot(self, filepath: Path | str) -> Path:
        """Save a JSON snapshot of all register values."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        snap = self.snapshot()
        filepath.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        self._log(f"Snapshot saved: {filepath}")
        return filepath

    def load_snapshot(self, filepath: Path | str) -> dict[str, str]:
        """Load register values from a JSON snapshot. Returns errors dict."""
        filepath = Path(filepath)
        snap = json.loads(filepath.read_text(encoding="utf-8"))
        values = snap.get("registers", {})
        # Only write writable registers
        writable = {r.name for r in writable_registers()}
        to_write = {k: v for k, v in values.items() if k in writable}
        errors = self.write_many(to_write)
        self._log(f"Loaded snapshot: {filepath.name} ({len(to_write)} registers)")
        return errors

    # ------------------------------------------------------------------
    # Change pars — compute coefficients and write
    # ------------------------------------------------------------------

    def change_pars(self, axis: str, host_params: dict[str, float],
                    pid_values: dict[str, float] | None = None) -> dict[str, str]:
        """Compute filter coefficients from host params and write to FPGA.

        Parameters
        ----------
        axis : "X", "Y", or "Z"
        host_params : current frequency/Q values from the GUI
        pid_values : optional dict of PID register values to write alongside
                     the computed coefficients (gains, limits, setpoints, etc.)

        Returns
        -------
        dict of {register_name: error_msg} for any write failures
        """
        coeffs = compute_coefficients(axis, host_params)
        to_write = dict(coeffs)
        if pid_values:
            to_write.update(pid_values)

        errors = self.write_many(to_write)
        ok = len(to_write) - len(errors)
        self._log(f"Change pars ({axis}): wrote {ok}/{len(to_write)} registers")
        _append_log("change_pars", {
            "axis": axis,
            "host_params": host_params,
            "coefficients": coeffs,
            "errors": errors,
        })
        return errors

    # ------------------------------------------------------------------
    # Ramping — gradually change a register value
    # ------------------------------------------------------------------

    def ramp_register(self, name: str, target: float, step: float,
                      delay_s: float, callback=None) -> threading.Thread:
        """Ramp a register from its current value to *target* in a background thread.

        Parameters
        ----------
        name     : register name
        target   : final register value
        step     : absolute step size per iteration (sign is determined automatically)
        delay_s  : seconds to wait between steps
        callback : optional callable(current_value) after each step

        Returns the started thread (for optional joining).
        """
        if step <= 0:
            raise ValueError("step must be > 0")

        def _ramp():
            current = self.read_register(name)
            direction = 1.0 if target > current else -1.0
            while self._connected:
                remaining = target - current
                if abs(remaining) <= abs(step):
                    self.write_register(name, target)
                    if callback:
                        callback(target)
                    break
                current += direction * step
                self.write_register(name, current)
                if callback:
                    callback(current)
                time.sleep(delay_s)
            self._log(f"Ramp {name} → {target} complete")

        t = threading.Thread(target=_ramp, daemon=True)
        t.start()
        _append_log("ramp_start", {
            "register": name, "target": target,
            "step": step, "delay_s": delay_s,
        })
        return t

    # ------------------------------------------------------------------
    # Arb waveform buffer loading
    # ------------------------------------------------------------------

    def write_arb_data(self, data) -> None:
        """Write a pre-loaded numpy array to the FPGA arb buffers.

        data : 1-D or 2-D array; up to 3 columns map to data_buffer_1/2/3.
        """
        import numpy as np
        data = np.asarray(data, dtype=float)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        n_samples, n_cols = data.shape
        buf_names = ["data_buffer_1", "data_buffer2", "data_buffer3"]
        self._log(f"Writing arb data: {n_samples} samples, {n_cols} ch")
        for i in range(n_samples):
            for _ in range(1000):
                if self.read_register("ready_to_write"):
                    break
                time.sleep(0.001)
            self.write_register("write_address", i)
            for c in range(min(n_cols, 3)):
                self.write_register(buf_names[c], data[i, c])
        self._log(f"Arb write complete: {n_samples} samples")

    def load_arb_waveform(self, filepath: Path | str) -> None:
        """Load waveform data from a text file and write to FPGA buffers.

        Accepts comma- or whitespace-delimited files with up to 3 columns
        corresponding to data_buffer_1, data_buffer2, and data_buffer3.
        """
        import numpy as np

        filepath = Path(filepath)
        with open(filepath, "r") as f:
            first_line = f.readline()
        delimiter = "," if "," in first_line else None
        data = np.loadtxt(str(filepath), delimiter=delimiter)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        self._log(f"Loading arb waveform: {filepath.name} ({data.shape[0]} samples, {data.shape[1]} ch)")
        self.write_arb_data(data)
        _append_log("arb_load", {"file": str(filepath), "samples": data.shape[0]})

    # ------------------------------------------------------------------
    # Save / load sphere parameters
    # ------------------------------------------------------------------

    def save_sphere(self, filepath: Path | str,
                    host_params: dict[str, float] | None = None) -> Path:
        """Save all register values and host params to a JSON file."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        snap = self.snapshot()
        if host_params:
            snap["host_params"] = host_params
        filepath.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        self._log(f"Sphere saved: {filepath}")
        return filepath

    def load_sphere(self, filepath: Path | str) -> tuple[dict[str, str], dict[str, float]]:
        """Load sphere parameters. Returns (write_errors, loaded_host_params)."""
        filepath = Path(filepath)
        snap = json.loads(filepath.read_text(encoding="utf-8"))
        # Write FPGA registers
        values = snap.get("registers", {})
        writable = {r.name for r in writable_registers()}
        to_write = {k: v for k, v in values.items() if k in writable}
        errors = self.write_many(to_write)
        # Return host params for GUI restoration
        host_params = snap.get("host_params", {})
        self._log(f"Sphere loaded: {filepath.name}")
        return errors, host_params

    # ------------------------------------------------------------------
    # Monitor (periodic polling of read-only indicators)
    # ------------------------------------------------------------------

    def start_monitor(self, plot_names: list[str] | None = None) -> None:
        """Begin polling registers. *plot_names* are read at fast rate."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._plot_names = plot_names or []
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        self._log("Monitor started")

    def stop_monitor(self) -> None:
        """Stop the polling monitor."""
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self._on_status(msg)

    def _read_one(self, name: str) -> float:
        """Read one register (caller holds _lock).

        For array registers nifpga returns a list; we return the first element
        so the existing scalar GUI display path keeps working.
        """
        if self._session is not None:
            try:
                val = self._session.registers[name].read()
                if isinstance(val, (list, tuple)):
                    return float(val[0]) if val else 0.0
                return 1.0 if val is True else (0.0 if val is False else float(val))
            except Exception:
                return 0.0
        else:
            sim = self._sim_regs.get(name, 0.0)
            return float(sim[0]) if isinstance(sim, list) else sim

    def _write_one(self, name: str, value, reg: RegisterDef) -> None:
        """Write one register (caller holds _lock).

        *value* may be a list for array registers (n_elements > 1).
        If a scalar is passed for an array register the value is placed in
        element 0 and the remaining elements are set to 0.0.
        """
        if self._session is not None:
            nifpga_reg = self._session.registers[name]
            if reg.is_bool:
                nifpga_reg.write(bool(value))
            elif reg.n_elements > 1:
                # Array FXP register — nifpga requires a list of the exact length
                if isinstance(value, (list, tuple)):
                    arr = list(value)
                else:
                    arr = [float(value)] + [0.0] * (reg.n_elements - 1)
                nifpga_reg.write(arr)
            elif reg.is_integer:
                nifpga_reg.write(int(round(float(value))))
            else:
                # FXP / SGL / DBL registers expect float.  If the LabVIEW VI
                # uses an integer type that isn't yet flagged with is_integer,
                # ctypes raises TypeError — catch it and retry as int so the
                # write succeeds; mark the register is_integer to fix it properly.
                try:
                    nifpga_reg.write(float(value))
                except TypeError:
                    nifpga_reg.write(int(round(float(value))))
                    self._log(
                        f"[type warning] {name!r} rejected float — "
                        "wrote as int. Set is_integer=True in fpga_registers.py.")
        else:
            self._sim_regs[name] = value

    def _monitor_loop(self) -> None:
        import time as _time
        _last_full = 0.0

        while not self._monitor_stop.is_set():
            plot_dt = max(0.001, self.config.plot_interval_ms / 1000.0)
            full_dt = max(0.001, self.config.poll_interval_ms / 1000.0)

            if self._connected:
                if self._on_plot_data and self._plot_names:
                    self._on_plot_data(self.read_registers(self._plot_names))

                now = _time.monotonic()
                if now - _last_full >= full_dt:
                    _last_full = now
                    if self._on_registers_updated:
                        self._on_registers_updated(self.read_all())

            self._monitor_stop.wait(plot_dt)
