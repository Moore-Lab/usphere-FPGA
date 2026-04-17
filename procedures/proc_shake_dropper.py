"""
procedures/proc_shake_dropper.py

Control procedure for Stage 5: dropper shaking.

Drives the dropper piezo through the Keysight 33500B AWG to shake
microspheres off the dropper tip and into the 1064 nm trapping beam.

The shaking sequence mirrors the LabVIEW dropper shaker VI:
  1. Configure the AWG for a continuous linear frequency sweep
     (default: 100 kHz → 700 kHz, 0.1 s per sweep).
  2. Enable AWG output.
  3. Step the AWG amplitude upward in equal increments, waiting a fixed
     dwell time between steps.
  4. After the last step (or when Stop is pressed), disable AWG output.

The operator monitors the BPD signal on the FPGA for the moment a sphere
falls into the trap, then presses Stop and moves to the next stage.

Default shaking parameters (from trapping-protocol Stage 5 LabVIEW VI):
  Start amplitude : 0.10 Vpp
  Step size       : 0.05 Vpp
  Number of steps : 50
  Dwell time      : 5.0 s/step
  → Max amplitude : 0.10 + 50 × 0.05 = 2.60 Vpp

Note on external amplifier
--------------------------
The LabVIEW VI also controls a voltage-controlled amplifier over COM5
(command format "VSET1: %.2f") in addition to the AWG sweep.  That device
is not yet represented by a module in this repo.  For the current iteration
the AWG amplitude is stepped directly; if the piezo drive chain requires the
amplifier for sufficient force, an ``mod_dropper_amplifier`` module should
be added and this procedure updated to step it in sync.

532 nm / BPD trap detection
----------------------------
The FPGA monitor feeds "AI X plot" and "AI Y plot" register values each
~200 ms cycle.  While the dropper is in the dropping position, the beam
is attenuated (moderate signal).  When a sphere is captured, the trap
modifies the signal slightly; the operator watches the display and confirms
trapping visually (camera + BPD) before pressing Stop.
"""

from __future__ import annotations

import datetime
import time

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

from modules import mod_keysight_awg as _awg
from procedures.base import ControlProcedure
from fpga_ipc import ShakeEventLogger


# ---------------------------------------------------------------------------
# Worker — AWG connection test
# ---------------------------------------------------------------------------

class _TestWorker(QThread):
    """Runs mod_keysight_awg.test() in a background thread."""

    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def run(self) -> None:
        ok, msg = _awg.test(self._config)
        self.finished.emit(ok, msg)


# ---------------------------------------------------------------------------
# Worker — single AWG command
# ---------------------------------------------------------------------------

class _CommandWorker(QThread):
    """Runs a single mod_keysight_awg.command() call."""

    finished = pyqtSignal(bool, str, float, bool)  # ok, msg, amplitude, output_on

    def __init__(self, config: dict, action: str, **kwargs):
        super().__init__()
        self._config = config
        self._action = action
        self._kwargs = kwargs

    def run(self) -> None:
        result = _awg.command(self._config, action=self._action, **self._kwargs)
        ok       = result.get("ok", False)
        msg      = result.get("message", "")
        amp      = float(result.get("amplitude_vpp", 0.0))
        out_on   = bool(result.get("output_on", False))
        self.finished.emit(ok, msg, amp, out_on)


# ---------------------------------------------------------------------------
# Worker — full amplitude-stepping shaking loop
# ---------------------------------------------------------------------------

class _ShakeWorker(QThread):
    """
    Runs the amplitude-stepping shaking sequence.

    Each step:
      1. Set AWG amplitude for this step.
      2. Wait dwell_s seconds (checking stop flag at 0.1 s intervals).

    Emits:
      step_update(step_number, amplitude_vpp, elapsed_s)  each step
      finished(ok, message)                               on completion / stop
    """

    step_update = pyqtSignal(int, float, float)   # step, amplitude_vpp, elapsed_s
    finished    = pyqtSignal(bool, str)

    def __init__(self, config: dict, n_steps: int, start_amp: float,
                 step_amp: float, dwell_s: float):
        super().__init__()
        self._config    = config
        self._n_steps   = n_steps
        self._start_amp = start_amp
        self._step_amp  = step_amp
        self._dwell_s   = dwell_s
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        t0 = time.monotonic()

        for i in range(self._n_steps):
            if self._stop_flag:
                self.finished.emit(False, f"Stopped at step {i} of {self._n_steps}.")
                return

            amp = self._start_amp + i * self._step_amp

            result = _awg.command(self._config, action="set_amplitude", amplitude_vpp=amp)
            if not result.get("ok"):
                self.finished.emit(False, f"set_amplitude failed at step {i}: {result.get('message')}")
                return

            elapsed = time.monotonic() - t0
            self.step_update.emit(i + 1, amp, elapsed)

            # Wait dwell_s in small slices so we can check the stop flag
            deadline = time.monotonic() + self._dwell_s
            while time.monotonic() < deadline:
                if self._stop_flag:
                    self.finished.emit(False, f"Stopped at step {i + 1} of {self._n_steps}.")
                    return
                time.sleep(0.05)

        elapsed = time.monotonic() - t0
        final_amp = self._start_amp + self._n_steps * self._step_amp
        self.finished.emit(
            True,
            f"Completed {self._n_steps} steps in {elapsed:.1f} s. "
            f"Final amplitude: {final_amp:.3f} Vpp.",
        )


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ShakeDropperWidget(QWidget):

    def __init__(self, procedure: "Procedure", parent=None):
        super().__init__(parent)
        self._procedure   = procedure
        self._worker: QThread | None = None
        self._connected   = False
        self._shaking     = False
        self._shake_worker: _ShakeWorker | None = None
        self._shake_logger = ShakeEventLogger()
        self._last_step    = 0
        self._last_amp     = 0.0

        self._build_ui()

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

        layout.addWidget(self._build_connection_group())
        layout.addWidget(self._build_sweep_params_group())
        layout.addWidget(self._build_shake_params_group())
        layout.addWidget(self._build_control_group())
        layout.addWidget(self._build_signal_group())
        layout.addWidget(self._build_log_group())
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _build_connection_group(self) -> QGroupBox:
        grp = QGroupBox("AWG Connection")
        l   = QVBoxLayout(grp)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("VISA resource:"))
        self._resource_edit = QLineEdit(_awg.CONFIG_FIELDS[0]["default"])
        self._resource_edit.setMinimumWidth(280)
        self._resource_edit.setToolTip(_awg.CONFIG_FIELDS[0]["tooltip"])
        r1.addWidget(self._resource_edit)

        r1.addWidget(QLabel("Ch:"))
        self._channel_spin = QSpinBox()
        self._channel_spin.setRange(1, 2)
        self._channel_spin.setValue(1)
        self._channel_spin.setFixedWidth(45)
        r1.addWidget(self._channel_spin)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedWidth(90)
        self._connect_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        self._connect_btn.clicked.connect(self._on_connect)
        r1.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFixedWidth(90)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        r1.addWidget(self._disconnect_btn)

        self._conn_status = QLabel("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        r1.addWidget(self._conn_status, stretch=1)
        l.addLayout(r1)

        return grp

    def _build_sweep_params_group(self) -> QGroupBox:
        grp = QGroupBox("Sweep Parameters")
        l   = QHBoxLayout(grp)

        for label, attr, default, suffix, lo, hi, step, dec in [
            ("Start freq:",  "_start_freq_spin", 100.0,  " kHz",  1.0,   30000.0, 10.0, 1),
            ("Stop freq:",   "_stop_freq_spin",  700.0,  " kHz",  1.0,   30000.0, 10.0, 1),
            ("Sweep time:",  "_sweep_time_spin",   0.1,  " s",    0.001,    10.0,  0.01, 3),
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

    def _build_shake_params_group(self) -> QGroupBox:
        grp = QGroupBox("Amplitude Ramp  (shaking parameters)")
        l   = QHBoxLayout(grp)

        for label, attr, default, suffix, lo, hi, step, dec in [
            ("Start amplitude:", "_start_amp_spin",  0.10, " Vpp", 0.001, 10.0, 0.01, 3),
            ("Step size:",       "_step_amp_spin",   0.05, " Vpp", 0.001,  5.0, 0.01, 3),
            ("Steps:",           "_n_steps_spin",    50,   "",     1,    500,   1,   0),
            ("Dwell time:",      "_dwell_spin",       5.0, " s",   0.1,  60.0,  0.5, 1),
        ]:
            l.addWidget(QLabel(label))
            if attr == "_n_steps_spin":
                spin = QSpinBox()
                spin.setRange(1, 500)
                spin.setValue(50)
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
            l.addWidget(spin)

        # Computed max amplitude label
        l.addWidget(QLabel("→ max:"))
        self._max_amp_lbl = QLabel("2.600 Vpp")
        self._max_amp_lbl.setStyleSheet("font-weight: bold;")
        l.addWidget(self._max_amp_lbl)
        l.addStretch()

        # Update max amplitude display when parameters change
        for attr in ("_start_amp_spin", "_step_amp_spin"):
            getattr(self, attr).valueChanged.connect(self._update_max_amp)
        self._n_steps_spin.valueChanged.connect(self._update_max_amp)

        return grp

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

        btn_row.addStretch()
        l.addLayout(btn_row)

        # Progress row
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

        progress_row.addWidget(QLabel("Amplitude:"))
        self._amp_lbl = QLabel("—")
        self._amp_lbl.setFixedWidth(90)
        self._amp_lbl.setStyleSheet("font-family: Consolas; font-weight: bold;")
        progress_row.addWidget(self._amp_lbl)

        progress_row.addWidget(QLabel("Elapsed:"))
        self._elapsed_lbl = QLabel("—")
        self._elapsed_lbl.setFixedWidth(70)
        self._elapsed_lbl.setStyleSheet("font-family: Consolas;")
        progress_row.addWidget(self._elapsed_lbl)

        l.addLayout(progress_row)
        return grp

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

    def _get_config(self) -> dict:
        return {
            "resource_name":  self._resource_edit.text().strip(),
            "channel":        self._channel_spin.value(),
            "start_freq_hz":  self._start_freq_spin.value() * 1e3,
            "stop_freq_hz":   self._stop_freq_spin.value() * 1e3,
            "sweep_time_s":   self._sweep_time_spin.value(),
            "amplitude_vpp":  self._start_amp_spin.value(),
        }

    def _update_max_amp(self) -> None:
        max_amp = (
            self._start_amp_spin.value()
            + self._n_steps_spin.value() * self._step_amp_spin.value()
        )
        self._max_amp_lbl.setText(f"{max_amp:.3f} Vpp")

    def _set_param_widgets_enabled(self, enabled: bool) -> None:
        for w in (
            self._resource_edit, self._channel_spin,
            self._start_freq_spin, self._stop_freq_spin, self._sweep_time_spin,
            self._start_amp_spin, self._step_amp_spin,
            self._n_steps_spin, self._dwell_spin,
        ):
            w.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _on_connect(self) -> None:
        self._connect_btn.setEnabled(False)
        self._conn_status.setText("Connecting…")
        self._conn_status.setStyleSheet("color: gray;")
        self._log(f"Connecting to '{self._resource_edit.text().strip()}' …")

        worker = _TestWorker(self._get_config())
        worker.finished.connect(self._on_connect_done)
        worker.start()
        self._worker = worker

    def _on_connect_done(self, ok: bool, msg: str) -> None:
        self._worker = None
        if ok:
            self._connected = True
            self._conn_status.setText("Connected")
            self._conn_status.setStyleSheet("color: green; font-weight: bold;")
            self._disconnect_btn.setEnabled(True)
            self._start_btn.setEnabled(True)
            self._log(f"Connected: {msg}")
        else:
            self._connected = False
            self._conn_status.setText(f"Failed")
            self._conn_status.setStyleSheet("color: red;")
            self._connect_btn.setEnabled(True)
            self._log(f"Connection failed: {msg}")

    def _on_disconnect(self) -> None:
        if self._shaking:
            self._on_stop()
        self._connected = False
        self._shaking   = False
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(False)
        self._connect_btn.setEnabled(True)
        self._conn_status.setText("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        self._set_param_widgets_enabled(True)
        self._log("Disconnected.")

    # ------------------------------------------------------------------
    # Shaking sequence
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if not self._connected:
            return

        config = self._get_config()

        # 1. Setup sweep + arm output
        self._log("Configuring AWG sweep…")
        self._start_btn.setEnabled(False)
        self._set_param_widgets_enabled(False)

        worker = _CommandWorker(config, action="setup_sweep",
                                amplitude_vpp=self._start_amp_spin.value())
        worker.finished.connect(self._on_setup_done)
        worker.start()
        self._worker = worker

    def _on_setup_done(self, ok: bool, msg: str, amp: float, out_on: bool) -> None:
        self._worker = None
        self._log(f"{'Setup OK' if ok else 'Setup FAILED'}: {msg}")

        if not ok:
            self._start_btn.setEnabled(True)
            self._set_param_widgets_enabled(True)
            return

        # 2. Output on
        config = self._get_config()
        worker = _CommandWorker(config, action="output_on")
        worker.finished.connect(self._on_output_on_done)
        worker.start()
        self._worker = worker

    def _on_output_on_done(self, ok: bool, msg: str, amp: float, out_on: bool) -> None:
        self._worker = None
        self._log(f"{'Output ON' if ok else 'Output ON FAILED'}: {msg}")

        if not ok:
            self._start_btn.setEnabled(True)
            self._set_param_widgets_enabled(True)
            return

        # 3. Begin amplitude-stepping loop
        n      = self._n_steps_spin.value()
        start  = self._start_amp_spin.value()
        step   = self._step_amp_spin.value()
        dwell  = self._dwell_spin.value()

        self._last_step = 0
        self._last_amp  = start
        self._shake_logger.start(amplitude_vpp=start, step=0)
        self._shaking = True
        self._stop_btn.setEnabled(True)
        self._progress_bar.setValue(0)
        self._step_lbl.setText(f"0 / {n}")
        self._amp_lbl.setText(f"{start:.3f} Vpp")
        self._elapsed_lbl.setText("0.0 s")

        total_s = n * dwell
        self._log(
            f"Shaking started: {n} steps × {step:.3f} Vpp, "
            f"{dwell:.1f} s/step  (~{total_s:.0f} s total)"
        )

        shake_worker = _ShakeWorker(
            self._get_config(), n, start, step, dwell
        )
        shake_worker.step_update.connect(self._on_step_update)
        shake_worker.finished.connect(self._on_shake_done)
        shake_worker.start()
        self._shake_worker = shake_worker

    def _on_step_update(self, step_n: int, amp_vpp: float, elapsed_s: float) -> None:
        n = self._n_steps_spin.value()
        self._step_lbl.setText(f"{step_n} / {n}")
        self._amp_lbl.setText(f"{amp_vpp:.3f} Vpp")
        self._elapsed_lbl.setText(f"{elapsed_s:.1f} s")
        pct = int(100 * step_n / n) if n > 0 else 0
        self._progress_bar.setValue(pct)
        self._last_step = step_n
        self._last_amp  = amp_vpp

    def _on_stop(self) -> None:
        if self._shake_worker is not None and self._shake_worker.isRunning():
            self._log("Stop requested — will halt after current dwell…")
            self._shake_worker.stop()

    def _on_shake_done(self, ok: bool, msg: str) -> None:
        self._shake_logger.stop(amplitude_vpp=self._last_amp, step=self._last_step)
        self._shaking = False
        self._shake_worker = None
        self._log(f"{'Done' if ok else 'Stopped'}: {msg}")

        self._stop_btn.setEnabled(False)
        self._progress_bar.setValue(100 if ok else self._progress_bar.value())

        # Turn output off
        config = self._get_config()
        self._log("Turning AWG output off…")
        worker = _CommandWorker(config, action="output_off")
        worker.finished.connect(self._on_output_off_done)
        worker.start()
        self._worker = worker

    def _on_output_off_done(self, ok: bool, msg: str, amp: float, out_on: bool) -> None:
        self._worker = None
        self._log(f"Output {'OFF' if ok else 'OFF FAILED'}: {msg}")
        self._start_btn.setEnabled(self._connected)
        self._set_param_widgets_enabled(True)

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
    DESCRIPTION = (
        "Stage 5 — Dropper shaking. "
        "Drives the dropper piezo via the Keysight 33500B AWG with a "
        "linear frequency sweep (100–700 kHz) and steps the amplitude "
        "upward to shake microspheres off the tip and into the trap. "
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

    def teardown(self) -> None:
        if self._widget is not None:
            self._widget.teardown()
