"""
procedures/proc_shake_dropper.py

Control procedure for Stage 5: dropper shaking.

Hardware chain
--------------
  Keysight 33500B AWG  →  MOSFET gate driver  →  TENMA 72-XXXX power supply  →  dropper piezo

The AWG drives a MOSFET gate with a continuous frequency sweep
(default: 100 kHz → 700 kHz, 0.1 s per sweep).  When the gate conducts,
the TENMA supply voltage appears across the piezo and shakes the dropper tip.
The AWG amplitude and sweep parameters are fixed throughout the sequence —
only the supply voltage is stepped.

Shaking sequence (per step)
----------------------------
  1. AWG output ON   (gate opens; piezo driven at current supply voltage)
  2. Wait sweep_time_s  (one frequency sweep completes)
  3. AWG output OFF
  4. Set supply to next voltage step (V += step_v, capped at max_v)
  5. Wait dwell_s   (pause before next step)
  Repeat for n_steps total steps.

The supply output stays on throughout (the gate controls actual drive).

Voltage ramp parameters
-----------------------
  Start voltage  : initial PSU set-point before first sweep trigger
  Step size      : PSU voltage increment after each sweep
  Steps          : total number of sweep–ramp cycles
  Dwell time     : pause after each voltage ramp (before next trigger)
  Max voltage    : hard cap applied to every set_voltage call (≤ 60 V)
  → Final voltage after last step = start + steps × step_size (capped at max)

State persistence
-----------------
The last voltage and step number are written to ipc/shake_dropper_state.json
after every completed step so the operator can see where the sequence was
left if the control program is restarted.  The value is displayed in gray
in the Voltage Ramp panel on startup.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import modules.mod_keysight_awg as _awg
import modules.mod_tenma_psu   as _psu
from procedures.base import ControlProcedure
from fpga.ipc import ShakeEventLogger

# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).parent.parent / "ipc" / "shake_dropper_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(voltage_v: float, step: int, total_steps: int, note: str = "") -> None:
    _STATE_FILE.parent.mkdir(exist_ok=True)
    state = _load_state()
    state["last_voltage_v"]    = round(voltage_v, 4)
    state["last_step"]         = step
    state["last_total_steps"]  = total_steps
    state["last_updated"]      = datetime.datetime.now().isoformat()
    if note:
        state["last_note"] = note
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Worker — AWG connection test
# ---------------------------------------------------------------------------

class _AWGTestWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def run(self) -> None:
        ok, msg = _awg.test(self._config)
        self.finished.emit(ok, msg)


# ---------------------------------------------------------------------------
# Worker — PSU connection test
# ---------------------------------------------------------------------------

class _PSUTestWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def run(self) -> None:
        ok, msg = _psu.test(self._config)
        self.finished.emit(ok, msg)


# ---------------------------------------------------------------------------
# Worker — single AWG command (output on/off)
# ---------------------------------------------------------------------------

class _AWGCommandWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict, action: str, **kwargs):
        super().__init__()
        self._config = config
        self._action = action
        self._kwargs = kwargs

    def run(self) -> None:
        result = _awg.command(self._config, action=self._action, **self._kwargs)
        self.finished.emit(result.get("ok", False), result.get("message", ""))


# ---------------------------------------------------------------------------
# Worker — single PSU command
# ---------------------------------------------------------------------------

class _PSUCommandWorker(QThread):
    finished = pyqtSignal(bool, str, float, bool)  # ok, msg, voltage_v, output_on

    def __init__(self, config: dict, action: str, **kwargs):
        super().__init__()
        self._config = config
        self._action = action
        self._kwargs = kwargs

    def run(self) -> None:
        result = _psu.command(self._config, action=self._action, **self._kwargs)
        self.finished.emit(
            result.get("ok", False),
            result.get("message", ""),
            float(result.get("voltage_v", 0.0)),
            bool(result.get("output_on", False)),
        )


# ---------------------------------------------------------------------------
# Worker — full shaking loop
# ---------------------------------------------------------------------------

class _ShakeWorker(QThread):
    """
    Executes the full sweep–ramp shaking sequence.

    Each step:
      1. AWG output ON
      2. Wait sweep_time_s  (one sweep)
      3. AWG output OFF
      4. Ramp PSU voltage to next step (capped at max_v)
      5. Save state to disk
      6. Wait dwell_s  (interruptible)

    Emits:
      sweep_start(step_n)                          before each AWG trigger
      step_update(step_n, voltage_v, elapsed_s)   after AWG off + PSU ramp, before dwell
      finished(ok, message)                        on completion or stop
    """

    sweep_start = pyqtSignal(int)                  # step number, before AWG on
    step_update = pyqtSignal(int, float, float)   # step, voltage_v, elapsed_s
    finished    = pyqtSignal(bool, str)

    def __init__(
        self,
        awg_config:   dict,
        psu_config:   dict,
        ch:           int,
        n_steps:      int,
        start_v:      float,
        step_v:       float,
        max_v:        float,
        dwell_s:      float,
        sweep_time_s: float,
        carrier_amp:  float,
        start_freq:   float,
        stop_freq:    float,
    ):
        super().__init__()
        self._awg_config   = awg_config
        self._psu_config   = psu_config
        self._ch           = ch
        self._n_steps      = n_steps
        self._start_v      = start_v
        self._step_v       = step_v
        self._max_v        = max_v
        self._dwell_s      = dwell_s
        self._sweep_time_s = sweep_time_s
        self._carrier_amp  = carrier_amp
        self._start_freq   = start_freq
        self._stop_freq    = stop_freq
        self._stop_flag    = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        t0 = time.monotonic()

        try:
            with _awg.open_awg(self._awg_config) as awg, \
                 _psu.open_psu(self._psu_config) as psu:

                # Configure AWG sweep once (fixed for entire sequence)
                ok = awg.setup_sweep(
                    channel    = self._ch,
                    start_freq = self._start_freq,
                    stop_freq  = self._stop_freq,
                    sweep_time = self._sweep_time_s,
                    waveform   = "sine",
                    amplitude  = self._carrier_amp,
                    spacing    = "linear",
                    trigger    = "immediate",
                )
                if not ok:
                    self.finished.emit(False, "AWG setup_sweep failed")
                    return

                # Set initial PSU voltage before first trigger
                current_v = min(self._start_v, self._max_v)
                if not psu.set_voltage(current_v):
                    self.finished.emit(False, f"PSU set_voltage({current_v:.2f} V) failed")
                    return

                for i in range(self._n_steps):
                    if self._stop_flag:
                        self.finished.emit(
                            False,
                            f"Stopped at step {i} of {self._n_steps}. "
                            f"Last voltage: {current_v:.2f} V.",
                        )
                        return

                    # 1. AWG trigger
                    self.sweep_start.emit(i + 1)
                    if not awg.output_on(self._ch):
                        self.finished.emit(False, f"AWG output_on failed at step {i+1}")
                        return

                    # 2. Wait one sweep
                    time.sleep(self._sweep_time_s)

                    # 3. AWG off
                    awg.output_off(self._ch)

                    # 4. Ramp PSU to next voltage
                    next_v = min(self._start_v + (i + 1) * self._step_v, self._max_v)
                    if not psu.set_voltage(next_v):
                        self.finished.emit(
                            False,
                            f"PSU set_voltage({next_v:.2f} V) failed at step {i+1}",
                        )
                        return

                    # Persist state after each completed step
                    elapsed = time.monotonic() - t0
                    _save_state(current_v, i + 1, self._n_steps,
                                note=f"completed step {i+1}/{self._n_steps}")
                    self.step_update.emit(i + 1, current_v, elapsed)

                    current_v = next_v

                    # 5. Dwell (interruptible)
                    deadline = time.monotonic() + self._dwell_s
                    while time.monotonic() < deadline:
                        if self._stop_flag:
                            self.finished.emit(
                                False,
                                f"Stopped during dwell at step {i+1} of {self._n_steps}. "
                                f"Last voltage: {current_v:.2f} V.",
                            )
                            return
                        time.sleep(0.05)

                elapsed = time.monotonic() - t0
                _save_state(
                    current_v, self._n_steps, self._n_steps,
                    note=f"completed all {self._n_steps} steps",
                )
                self.finished.emit(
                    True,
                    f"Completed {self._n_steps} steps in {elapsed:.1f} s. "
                    f"Final voltage: {current_v:.2f} V.",
                )

        except Exception as exc:
            self.finished.emit(False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ShakeDropperWidget(QWidget):

    def __init__(self, procedure: "Procedure", parent=None):
        super().__init__(parent)
        self._procedure    = procedure
        self._worker: QThread | None       = None
        self._shake_worker: _ShakeWorker | None = None
        self._awg_connected = False
        self._psu_connected = False
        self._shaking        = False
        self._shake_logger   = ShakeEventLogger()
        self._last_step      = 0
        self._last_voltage   = 0.0
        self._active_n_steps = 0    # actual steps in current run (may differ from spinbox for resume)
        self._shake_event_cb = None  # set by trapping panel via set_shake_event_callback

        self._build_ui()
        self._load_last_session()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        layout.addWidget(self._build_awg_connection_group())
        layout.addWidget(self._build_psu_connection_group())
        layout.addWidget(self._build_sweep_params_group())
        layout.addWidget(self._build_voltage_ramp_group())
        layout.addWidget(self._build_control_group())
        layout.addWidget(self._build_signal_group())
        layout.addWidget(self._build_log_group())
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # --- AWG connection ---

    def _build_awg_connection_group(self) -> QGroupBox:
        grp = QGroupBox("AWG Connection  (Keysight 33500B)")
        l   = QVBoxLayout(grp)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("VISA resource:"))
        self._awg_resource_edit = QLineEdit(_awg.CONFIG_FIELDS[0]["default"])
        self._awg_resource_edit.setMinimumWidth(280)
        self._awg_resource_edit.setToolTip(_awg.CONFIG_FIELDS[0]["tooltip"])
        r1.addWidget(self._awg_resource_edit)

        r1.addWidget(QLabel("Ch:"))
        self._channel_spin = QSpinBox()
        self._channel_spin.setRange(1, 2)
        self._channel_spin.setValue(1)
        self._channel_spin.setFixedWidth(45)
        r1.addWidget(self._channel_spin)

        self._awg_connect_btn = QPushButton("Connect")
        self._awg_connect_btn.setFixedWidth(90)
        self._awg_connect_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        self._awg_connect_btn.clicked.connect(self._on_awg_connect)
        r1.addWidget(self._awg_connect_btn)

        self._awg_disconnect_btn = QPushButton("Disconnect")
        self._awg_disconnect_btn.setFixedWidth(90)
        self._awg_disconnect_btn.setEnabled(False)
        self._awg_disconnect_btn.clicked.connect(self._on_awg_disconnect)
        r1.addWidget(self._awg_disconnect_btn)

        self._awg_status = QLabel("Disconnected")
        self._awg_status.setStyleSheet("color: gray;")
        r1.addWidget(self._awg_status, stretch=1)
        l.addLayout(r1)
        return grp

    # --- PSU connection ---

    def _build_psu_connection_group(self) -> QGroupBox:
        grp = QGroupBox("Power Supply Connection  (TENMA 72-XXXX)")
        l   = QVBoxLayout(grp)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Serial port:"))
        self._psu_port_edit = QLineEdit(_psu.CONFIG_FIELDS[0]["default"])
        self._psu_port_edit.setFixedWidth(80)
        self._psu_port_edit.setToolTip(_psu.CONFIG_FIELDS[0]["tooltip"])
        r1.addWidget(self._psu_port_edit)

        self._psu_connect_btn = QPushButton("Connect")
        self._psu_connect_btn.setFixedWidth(90)
        self._psu_connect_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        self._psu_connect_btn.clicked.connect(self._on_psu_connect)
        r1.addWidget(self._psu_connect_btn)

        self._psu_disconnect_btn = QPushButton("Disconnect")
        self._psu_disconnect_btn.setFixedWidth(90)
        self._psu_disconnect_btn.setEnabled(False)
        self._psu_disconnect_btn.clicked.connect(self._on_psu_disconnect)
        r1.addWidget(self._psu_disconnect_btn)

        # Output toggle
        self._psu_output_btn = QPushButton("Output ON")
        self._psu_output_btn.setFixedWidth(100)
        self._psu_output_btn.setEnabled(False)
        self._psu_output_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; "
            "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #22c55e; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._psu_output_btn.clicked.connect(self._on_psu_output_toggle)
        self._psu_output_on = False
        r1.addWidget(self._psu_output_btn)

        self._psu_status = QLabel("Disconnected")
        self._psu_status.setStyleSheet("color: gray;")
        r1.addWidget(self._psu_status, stretch=1)
        l.addLayout(r1)
        return grp

    # --- Sweep params ---

    def _build_sweep_params_group(self) -> QGroupBox:
        grp = QGroupBox("Sweep Parameters  (AWG — fixed during shaking)")
        l   = QHBoxLayout(grp)

        for label, attr, default, suffix, lo, hi, step, dec in [
            ("Start freq:",     "_start_freq_spin",  100.0,  " kHz", 1.0,   30000.0, 10.0,  1),
            ("Stop freq:",      "_stop_freq_spin",   700.0,  " kHz", 1.0,   30000.0, 10.0,  1),
            ("Sweep time:",     "_sweep_time_spin",    0.1,  " s",   0.001,    10.0,  0.01,  3),
            ("Carrier amp:",    "_carrier_amp_spin",   0.1,  " Vpp", 0.001,    10.0,  0.01,  3),
        ]:
            l.addWidget(QLabel(label))
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setValue(default)
            spin.setDecimals(dec)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setFixedWidth(120)
            setattr(self, attr, spin)
            l.addWidget(spin)

        l.addStretch()
        return grp

    # --- Voltage ramp ---

    def _build_voltage_ramp_group(self) -> QGroupBox:
        grp = QGroupBox("Voltage Ramp  (PSU set-point steps)")
        l   = QVBoxLayout(grp)

        param_row = QHBoxLayout()
        for label, attr, default, suffix, lo, hi, step, dec in [
            ("Start voltage:", "_start_v_spin",  5.0,  " V",  0.0,  60.0,  0.5,  2),
            ("Step size:",     "_step_v_spin",   2.0,  " V",  0.01, 30.0,  0.5,  2),
            ("Steps:",         "_n_steps_spin",  10,   "",    1,    500,   1,    0),
            ("Dwell time:",    "_dwell_spin",     5.0, " s",  0.1,  120.0, 0.5,  1),
            ("Max voltage:",   "_max_v_spin",    60.0, " V",  0.0,  60.0,  1.0,  1),
        ]:
            param_row.addWidget(QLabel(label))
            if attr == "_n_steps_spin":
                spin = QSpinBox()
                spin.setRange(1, 500)
                spin.setValue(10)
                spin.setFixedWidth(70)
            else:
                spin = QDoubleSpinBox()
                spin.setRange(lo, hi)
                spin.setValue(default)
                spin.setDecimals(dec)
                spin.setSingleStep(step)
                spin.setSuffix(suffix)
                spin.setFixedWidth(100)
            setattr(self, attr, spin)
            param_row.addWidget(spin)

        param_row.addWidget(QLabel("→ max:"))
        self._computed_max_lbl = QLabel("25.00 V")
        self._computed_max_lbl.setStyleSheet("font-weight: bold;")
        param_row.addWidget(self._computed_max_lbl)
        param_row.addStretch()
        l.addLayout(param_row)

        for attr in ("_start_v_spin", "_step_v_spin"):
            getattr(self, attr).valueChanged.connect(self._update_computed_max)
        self._n_steps_spin.valueChanged.connect(self._update_computed_max)

        # Last-session hint
        self._last_session_lbl = QLabel("")
        self._last_session_lbl.setStyleSheet("color: #9ca3af; font-size: 10px;")
        l.addWidget(self._last_session_lbl)

        self._update_computed_max()
        return grp

    # --- Control ---

    def _build_control_group(self) -> QGroupBox:
        grp = QGroupBox("Shaking Control")
        l   = QVBoxLayout(grp)

        btn_row = QHBoxLayout()

        self._start_btn = QPushButton("▶  Start Shaking")
        self._start_btn.setFixedHeight(38)
        self._start_btn.setMinimumWidth(160)
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; "
            "font-size: 13px; font-weight: bold; border-radius: 4px; }"
            "QPushButton:hover { background-color: #22c55e; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setFixedHeight(38)
        self._stop_btn.setMinimumWidth(100)
        self._stop_btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-size: 13px; font-weight: bold; border-radius: 4px; }"
            "QPushButton:hover { background-color: #ef4444; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)

        self._resume_btn = QPushButton("⏩  Resume")
        self._resume_btn.setFixedHeight(38)
        self._resume_btn.setMinimumWidth(130)
        self._resume_btn.setStyleSheet(
            "QPushButton { background-color: #d97706; color: white; "
            "font-size: 13px; font-weight: bold; border-radius: 4px; }"
            "QPushButton:hover { background-color: #f59e0b; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._resume_btn.setEnabled(False)
        self._resume_btn.setToolTip("Resume from last session's voltage and step")
        self._resume_btn.clicked.connect(self._on_resume)
        btn_row.addWidget(self._resume_btn)

        btn_row.addStretch()
        l.addLayout(btn_row)

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel("Progress:"))
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setMinimumWidth(200)
        progress_row.addWidget(self._progress_bar, stretch=1)

        progress_row.addWidget(QLabel("Step:"))
        self._step_lbl = QLabel("—")
        self._step_lbl.setFixedWidth(80)
        self._step_lbl.setStyleSheet("font-family: Consolas; font-weight: bold;")
        progress_row.addWidget(self._step_lbl)

        progress_row.addWidget(QLabel("Voltage:"))
        self._volt_lbl = QLabel("—")
        self._volt_lbl.setFixedWidth(80)
        self._volt_lbl.setStyleSheet("font-family: Consolas; font-weight: bold;")
        progress_row.addWidget(self._volt_lbl)

        progress_row.addWidget(QLabel("Elapsed:"))
        self._elapsed_lbl = QLabel("—")
        self._elapsed_lbl.setFixedWidth(70)
        self._elapsed_lbl.setStyleSheet("font-family: Consolas;")
        progress_row.addWidget(self._elapsed_lbl)

        l.addLayout(progress_row)
        return grp

    # --- BPD signal ---

    def _build_signal_group(self) -> QGroupBox:
        grp = QGroupBox("BPD Signal  (from FPGA — watch for trap)")
        l   = QVBoxLayout(grp)

        val_row = QHBoxLayout()
        val_row.addWidget(QLabel("X BPD  (AI X plot):"))
        self._xbpd_lbl = QLabel("—")
        self._xbpd_lbl.setStyleSheet("font-family: Consolas; font-weight: bold;")
        self._xbpd_lbl.setFixedWidth(100)
        val_row.addWidget(self._xbpd_lbl)

        val_row.addWidget(QLabel("Y BPD  (AI Y plot):"))
        self._ybpd_lbl = QLabel("—")
        self._ybpd_lbl.setStyleSheet("font-family: Consolas; font-weight: bold;")
        self._ybpd_lbl.setFixedWidth(100)
        val_row.addWidget(self._ybpd_lbl)

        self._bpd_indicator = QLabel("FPGA not connected")
        self._bpd_indicator.setAlignment(Qt.AlignCenter)
        self._bpd_indicator.setFixedWidth(200)
        self._bpd_indicator.setStyleSheet(
            "background: #e5e7eb; color: #6b7280; padding: 3px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 11px;"
        )
        val_row.addWidget(self._bpd_indicator)
        val_row.addStretch()
        l.addLayout(val_row)

        hint = QLabel(
            "During shaking the signal is attenuated (dropper in beam path). "
            "A trapped sphere may cause a shift — confirm on camera before stopping."
        )
        hint.setStyleSheet("color: gray; font-size: 10px;")
        hint.setWordWrap(True)
        l.addWidget(hint)
        return grp

    # --- Log ---

    def _build_log_group(self) -> QGroupBox:
        grp = QGroupBox("Log")
        l   = QVBoxLayout(grp)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(180)
        self._log_box.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 10px;")
        l.addWidget(self._log_box)
        return grp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_box.append(f"[{ts}] {msg}")
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _awg_config(self) -> dict:
        return {
            "resource_name":  self._awg_resource_edit.text().strip(),
            "channel":        self._channel_spin.value(),
            "start_freq_hz":  self._start_freq_spin.value() * 1e3,
            "stop_freq_hz":   self._stop_freq_spin.value()  * 1e3,
            "sweep_time_s":   self._sweep_time_spin.value(),
            "amplitude_vpp":  self._carrier_amp_spin.value(),
        }

    def _psu_config(self) -> dict:
        return {
            "serial_port": self._psu_port_edit.text().strip(),
            "baud_rate":   9600,
        }

    def _update_computed_max(self) -> None:
        computed = (
            self._start_v_spin.value()
            + self._n_steps_spin.value() * self._step_v_spin.value()
        )
        capped = min(computed, self._max_v_spin.value())
        self._computed_max_lbl.setText(f"{capped:.2f} V")

    def _update_start_btn(self) -> None:
        can_run = self._awg_connected and self._psu_connected and not self._shaking
        self._start_btn.setEnabled(can_run)
        state = _load_state()
        remaining = max(0, state.get("last_total_steps", 0) - state.get("last_step", 0))
        self._resume_btn.setEnabled(can_run and remaining > 0)

    def _set_param_widgets_enabled(self, enabled: bool) -> None:
        for w in (
            self._awg_resource_edit, self._channel_spin,
            self._start_freq_spin, self._stop_freq_spin,
            self._sweep_time_spin, self._carrier_amp_spin,
            self._psu_port_edit,
            self._start_v_spin, self._step_v_spin,
            self._n_steps_spin, self._dwell_spin, self._max_v_spin,
        ):
            w.setEnabled(enabled)

    def _load_last_session(self) -> None:
        state = _load_state()
        if not state:
            return
        v     = state.get("last_voltage_v")
        step  = state.get("last_step")
        total = state.get("last_total_steps")
        ts    = state.get("last_updated", "")
        if v is None:
            return
        ts_fmt = ""
        if ts:
            try:
                dt = datetime.datetime.fromisoformat(ts)
                ts_fmt = f" ({dt.strftime('%Y-%m-%d %H:%M')})"
            except ValueError:
                pass
        step_str = f"  step {step} / {total}" if step is not None and total is not None else ""
        self._last_session_lbl.setText(
            f"Last session{ts_fmt}: left at {v:.2f} V{step_str}"
        )

    # ------------------------------------------------------------------
    # AWG connection
    # ------------------------------------------------------------------

    def _on_awg_connect(self) -> None:
        self._awg_connect_btn.setEnabled(False)
        self._awg_status.setText("Connecting…")
        self._awg_status.setStyleSheet("color: gray;")
        self._log(f"AWG: connecting to '{self._awg_resource_edit.text().strip()}' …")
        worker = _AWGTestWorker(self._awg_config())
        worker.finished.connect(self._on_awg_connect_done)
        worker.start()
        self._worker = worker

    def _on_awg_connect_done(self, ok: bool, msg: str) -> None:
        self._worker = None
        if ok:
            self._awg_connected = True
            self._awg_status.setText("Connected")
            self._awg_status.setStyleSheet("color: green; font-weight: bold;")
            self._awg_disconnect_btn.setEnabled(True)
            self._log(f"AWG connected: {msg}")
        else:
            self._awg_connected = False
            self._awg_status.setText("Failed")
            self._awg_status.setStyleSheet("color: red;")
            self._awg_connect_btn.setEnabled(True)
            self._log(f"AWG connection failed: {msg}")
        self._update_start_btn()

    def _on_awg_disconnect(self) -> None:
        if self._shaking:
            self._on_stop()
        self._awg_connected = False
        self._awg_disconnect_btn.setEnabled(False)
        self._awg_connect_btn.setEnabled(True)
        self._awg_status.setText("Disconnected")
        self._awg_status.setStyleSheet("color: gray;")
        self._update_start_btn()
        self._log("AWG disconnected.")

    # ------------------------------------------------------------------
    # PSU connection
    # ------------------------------------------------------------------

    def _on_psu_connect(self) -> None:
        self._psu_connect_btn.setEnabled(False)
        self._psu_status.setText("Connecting…")
        self._psu_status.setStyleSheet("color: gray;")
        self._log(f"PSU: connecting on '{self._psu_port_edit.text().strip()}' …")
        worker = _PSUTestWorker(self._psu_config())
        worker.finished.connect(self._on_psu_connect_done)
        worker.start()
        self._worker = worker

    def _on_psu_connect_done(self, ok: bool, msg: str) -> None:
        self._worker = None
        if ok:
            self._psu_connected = True
            self._psu_status.setText("Connected")
            self._psu_status.setStyleSheet("color: green; font-weight: bold;")
            self._psu_disconnect_btn.setEnabled(True)
            self._psu_output_btn.setEnabled(True)
            self._log(f"PSU connected: {msg}")
        else:
            self._psu_connected = False
            self._psu_status.setText("Failed")
            self._psu_status.setStyleSheet("color: red;")
            self._psu_connect_btn.setEnabled(True)
            self._log(f"PSU connection failed: {msg}")
        self._update_start_btn()

    def _on_psu_disconnect(self) -> None:
        if self._shaking:
            self._on_stop()
        self._psu_connected = False
        self._psu_output_on = False
        self._psu_disconnect_btn.setEnabled(False)
        self._psu_connect_btn.setEnabled(True)
        self._psu_output_btn.setEnabled(False)
        self._psu_output_btn.setText("Output ON")
        self._psu_status.setText("Disconnected")
        self._psu_status.setStyleSheet("color: gray;")
        self._update_start_btn()
        self._log("PSU disconnected.")

    def _on_psu_output_toggle(self) -> None:
        action = "output_off" if self._psu_output_on else "output_on"
        self._psu_output_btn.setEnabled(False)
        worker = _PSUCommandWorker(self._psu_config(), action=action)
        worker.finished.connect(self._on_psu_output_done)
        worker.start()
        self._worker = worker

    def _on_psu_output_done(self, ok: bool, msg: str, voltage_v: float, output_on: bool) -> None:
        self._worker = None
        self._psu_output_on = output_on
        self._psu_output_btn.setEnabled(self._psu_connected and not self._shaking)
        if output_on:
            self._psu_output_btn.setText("Output OFF")
            self._psu_output_btn.setStyleSheet(
                "QPushButton { background-color: #dc2626; color: white; "
                "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
                "QPushButton:hover { background-color: #ef4444; }"
                "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
            )
        else:
            self._psu_output_btn.setText("Output ON")
            self._psu_output_btn.setStyleSheet(
                "QPushButton { background-color: #16a34a; color: white; "
                "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
                "QPushButton:hover { background-color: #22c55e; }"
                "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
            )
        self._log(f"PSU output: {msg}")

    # ------------------------------------------------------------------
    # Shaking sequence
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        self._start_internal(
            start_v=self._start_v_spin.value(),
            n_steps=self._n_steps_spin.value(),
            label="Shaking started",
        )

    def _on_resume(self) -> None:
        state = _load_state()
        remaining = max(0, state.get("last_total_steps", 0) - state.get("last_step", 0))
        if remaining == 0:
            return
        last_v = float(state.get("last_voltage_v", self._start_v_spin.value()))
        self._start_internal(start_v=last_v, n_steps=remaining, label="Resumed")

    def _start_internal(self, start_v: float, n_steps: int,
                        label: str = "Shaking started") -> None:
        if not (self._awg_connected and self._psu_connected):
            return

        awg_cfg = self._awg_config()
        psu_cfg = self._psu_config()

        step  = self._step_v_spin.value()
        max_v = self._max_v_spin.value()
        dwell = self._dwell_spin.value()
        ch    = self._channel_spin.value()

        self._shaking        = True
        self._active_n_steps = n_steps
        self._last_step      = 0
        self._last_voltage   = start_v
        self._start_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._psu_output_btn.setEnabled(False)
        self._set_param_widgets_enabled(False)
        self._progress_bar.setValue(0)
        self._step_lbl.setText(f"0 / {n_steps}")
        self._volt_lbl.setText(f"{start_v:.2f} V")
        self._elapsed_lbl.setText("0.0 s")

        if self._shake_event_cb:
            self._shake_event_cb("start")
        self._shake_logger.start(amplitude_vpp=start_v, step=0)
        total_s = n_steps * (awg_cfg["sweep_time_s"] + dwell)
        self._log(
            f"{label}: {n_steps} steps, "
            f"{start_v:.2f}–{min(start_v + n_steps * step, max_v):.2f} V "
            f"(step {step:.2f} V), {dwell:.1f} s dwell  (~{total_s:.0f} s total)"
        )

        shake_worker = _ShakeWorker(
            awg_config   = awg_cfg,
            psu_config   = psu_cfg,
            ch           = ch,
            n_steps      = n_steps,
            start_v      = start_v,
            step_v       = step,
            max_v        = max_v,
            dwell_s      = dwell,
            sweep_time_s = awg_cfg["sweep_time_s"],
            carrier_amp  = awg_cfg["amplitude_vpp"],
            start_freq   = awg_cfg["start_freq_hz"],
            stop_freq    = awg_cfg["stop_freq_hz"],
        )
        shake_worker.sweep_start.connect(self._on_sweep_start)
        shake_worker.step_update.connect(self._on_step_update)
        shake_worker.finished.connect(self._on_shake_done)
        shake_worker.start()
        self._shake_worker = shake_worker

    def _on_sweep_start(self, step_n: int) -> None:
        if self._shake_event_cb:
            self._shake_event_cb("sweep_start")

    def _on_step_update(self, step_n: int, voltage_v: float, elapsed_s: float) -> None:
        if self._shake_event_cb:
            self._shake_event_cb("step")
        n = self._active_n_steps or self._n_steps_spin.value()
        self._step_lbl.setText(f"{step_n} / {n}")
        self._volt_lbl.setText(f"{voltage_v:.2f} V")
        self._elapsed_lbl.setText(f"{elapsed_s:.1f} s")
        self._progress_bar.setValue(int(100 * step_n / n) if n > 0 else 0)
        self._last_step    = step_n
        self._last_voltage = voltage_v
        self._last_session_lbl.setText(
            f"Current session: step {step_n} / {n}, voltage {voltage_v:.2f} V"
        )

    def _on_stop(self) -> None:
        if self._shake_worker is not None and self._shake_worker.isRunning():
            self._log("Stop requested — will halt after current sweep…")
            self._shake_worker.stop()

    def _on_shake_done(self, ok: bool, msg: str) -> None:
        if self._shake_event_cb:
            self._shake_event_cb("done")
        self._shake_logger.stop(
            amplitude_vpp=self._last_voltage, step=self._last_step
        )
        self._shaking      = False
        self._shake_worker = None
        self._log(f"{'Done' if ok else 'Stopped'}: {msg}")

        self._stop_btn.setEnabled(False)
        self._progress_bar.setValue(100 if ok else self._progress_bar.value())
        self._update_start_btn()
        self._psu_output_btn.setEnabled(self._psu_connected)
        self._set_param_widgets_enabled(True)
        self._load_last_session()

    # ------------------------------------------------------------------
    # FPGA update — BPD display
    # ------------------------------------------------------------------

    def on_fpga_update(self, state: dict[str, float]) -> None:
        x = state.get("AI X plot", None)
        y = state.get("AI Y plot", None)

        if x is not None:
            self._xbpd_lbl.setText(f"{x:+.4f}")
        if y is not None:
            self._ybpd_lbl.setText(f"{y:+.4f}")

        if x is not None and y is not None:
            amplitude = abs(x) + abs(y)
            if amplitude < 0.05:
                text, bg, fg = "BLOCKED", "#fef3c7", "#92400e"
            elif amplitude > 1.5:
                text, bg, fg = "OPEN BEAM PATH", "#dbeafe", "#1e40af"
            else:
                text, bg, fg = "SHAKING / DROPPING", "#dcfce7", "#166534"
            self._bpd_indicator.setText(text)
            self._bpd_indicator.setStyleSheet(
                f"background: {bg}; color: {fg}; padding: 3px 8px; "
                f"border-radius: 4px; font-weight: bold; font-size: 11px;"
            )

    # ------------------------------------------------------------------
    # Public API for trapping panel
    # ------------------------------------------------------------------

    def set_shake_event_callback(self, fn) -> None:
        """Register a callable(event_type) called on 'start', 'sweep_start', 'step', 'done'."""
        self._shake_event_cb = fn

    def request_stop(self) -> None:
        """Stop shaking (safe to call on the main thread)."""
        self._on_stop()

    def is_shaking(self) -> bool:
        return self._shaking

    def start_shaking_public(self, start_v: float, step_v: float,
                             n_steps: int, dwell_s: float,
                             max_v: float) -> bool:
        """Start shaking with given params. Returns False if not connected or already shaking."""
        if not (self._awg_connected and self._psu_connected) or self._shaking:
            return False
        self._start_v_spin.setValue(start_v)
        self._step_v_spin.setValue(step_v)
        self._n_steps_spin.setValue(n_steps)
        self._dwell_spin.setValue(dwell_s)
        self._max_v_spin.setValue(max_v)
        self._start_internal(start_v=start_v, n_steps=n_steps)
        return True

    def resume_shaking_public(self) -> bool:
        """Resume from last session state. Returns False if nothing to resume or not connected."""
        if not (self._awg_connected and self._psu_connected) or self._shaking:
            return False
        state = _load_state()
        remaining = max(0, state.get("last_total_steps", 0) - state.get("last_step", 0))
        if remaining == 0:
            return False
        last_v = float(state.get("last_voltage_v", self._start_v_spin.value()))
        self._start_internal(start_v=last_v, n_steps=remaining, label="Resumed")
        return True

    # ------------------------------------------------------------------
    # Session state
    # ------------------------------------------------------------------

    def get_ui_state(self) -> dict:
        """Return all spinbox values for unified session state persistence."""
        return {
            "start_v":         self._start_v_spin.value(),
            "step_v":          self._step_v_spin.value(),
            "n_steps":         self._n_steps_spin.value(),
            "dwell_s":         self._dwell_spin.value(),
            "max_v":           self._max_v_spin.value(),
            "start_freq_khz":  self._start_freq_spin.value(),
            "stop_freq_khz":   self._stop_freq_spin.value(),
            "sweep_time_s":    self._sweep_time_spin.value(),
            "carrier_amp_vpp": self._carrier_amp_spin.value(),
            "channel":         self._channel_spin.value(),
        }

    def restore_ui_state(self, state: dict) -> None:
        """Restore spinbox values from a unified session state dict."""
        def _s(attr, key, cast=float):
            if key in state:
                try:
                    getattr(self, attr).setValue(cast(state[key]))
                except Exception:
                    pass
        _s("_start_v_spin",     "start_v")
        _s("_step_v_spin",      "step_v")
        _s("_n_steps_spin",     "n_steps", int)
        _s("_dwell_spin",       "dwell_s")
        _s("_max_v_spin",       "max_v")
        _s("_start_freq_spin",  "start_freq_khz")
        _s("_stop_freq_spin",   "stop_freq_khz")
        _s("_sweep_time_spin",  "sweep_time_s")
        _s("_carrier_amp_spin", "carrier_amp_vpp")
        _s("_channel_spin",     "channel", int)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        if self._shake_worker is not None and self._shake_worker.isRunning():
            self._shake_worker.stop()
            self._shake_worker.wait(5000)
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)


# ---------------------------------------------------------------------------
# Procedure class
# ---------------------------------------------------------------------------

class Procedure(ControlProcedure):
    NAME        = "Shake Dropper"
    PERSISTENT  = True   # always loaded as a dedicated tab; excluded from Procedures list
    DESCRIPTION = (
        "Stage 5 — Dropper shaking. "
        "The Keysight 33500B AWG drives a MOSFET gate with a fixed frequency sweep "
        "(100–700 kHz); the TENMA power supply voltage is stepped upward each cycle "
        "to progressively shake microspheres off the dropper tip into the trap. "
        "Displays live BPD signal from the FPGA to monitor for a trapped sphere."
    )

    def __init__(self):
        self._widget: ShakeDropperWidget | None = None

    def create_widget(self, parent=None) -> QWidget:
        self._widget = ShakeDropperWidget(self, parent)
        return self._widget

    def on_fpga_update(self, state: dict[str, float]) -> None:
        if self._widget is not None:
            self._widget.on_fpga_update(state)

    def get_ui_state(self) -> dict:
        if self._widget is not None:
            return self._widget.get_ui_state()
        return {}

    def restore_ui_state(self, state: dict) -> None:
        if self._widget is not None:
            self._widget.restore_ui_state(state)

    def teardown(self) -> None:
        if self._widget is not None:
            self._widget.teardown()
