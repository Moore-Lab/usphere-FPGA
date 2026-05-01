"""
procedures/proc_position_dropper.py

Control procedure for the dropper translation stage:
  Thorlabs Z812 linear actuator + KDC101 KCube DC Servo Motor Controller.

Features
--------
  - Connect / test the KDC101 (non-persistent — each command opens and closes)
  - One-click preset moves: Retrieval, Dropping, Retraction
  - Home, absolute move, and jog (forward / reverse)
  - Editable preset positions and motion parameters
  - Live 532 nm alignment signal readback from FPGA registers

532 nm alignment signal
-----------------------
The 532 nm alignment laser is co-aligned with the 1064 nm trapping beam and
detected on the X and Y balanced photodiodes (BPD).  As the stage moves
inward from the retracted position, the signal transitions through three
states:

  Retracted  → strong signal (open beam path)
  Chassis    → near zero (chassis wall blocks the beam)
  Dropping   → attenuated signal (beam passes through aperture and dropper tip)

The FPGA indicator registers "AI X plot" and "AI Y plot" carry these values
at the monitor poll rate.  The alignment indicator in this widget colours
green when the dropping-position signal is detected and grey otherwise.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modules import mod_dropper_stage as _stage
from procedures.base import ControlProcedure


# ---------------------------------------------------------------------------
# Worker thread — runs a single blocking module command
# ---------------------------------------------------------------------------

class _CommandWorker(QThread):
    """Runs mod_dropper_stage.command() in a background thread."""

    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str, float)   # ok, message, position_mm

    def __init__(self, config: dict, action: str, **kwargs):
        super().__init__()
        self._config = config
        self._action = action
        self._kwargs = kwargs

    def run(self) -> None:
        result = _stage.command(self._config, action=self._action, **self._kwargs)
        ok  = result.get("ok", False)
        msg = result.get("message", "")
        pos = float(result.get("position_mm", 0.0))
        self.finished.emit(ok, msg, pos)


class _ReadWorker(QThread):
    """Reads current position + status from the stage."""

    finished = pyqtSignal(bool, str)   # ok, status_message

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def run(self) -> None:
        ok, msg = _stage.test(self._config)
        self.finished.emit(ok, msg)


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class DropperStageWidget(QWidget):

    def __init__(self, procedure: "Procedure", parent=None):
        super().__init__(parent)
        self._procedure   = procedure
        self._worker: QThread | None = None
        self._connected   = False      # True after a successful test
        self._position_mm = 0.0
        self._is_homed    = False

        self._build_ui()
        self._restore_last_position()

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
        layout.addWidget(self._build_presets_group())
        layout.addWidget(self._build_manual_group())
        layout.addWidget(self._build_motion_params_group())
        layout.addWidget(self._build_signal_group())
        layout.addWidget(self._build_log_group())
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _build_connection_group(self) -> QGroupBox:
        grp = QGroupBox("Connection")
        l   = QVBoxLayout(grp)

        # Serial number + buttons row
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Serial number:"))
        self._sn_edit = QLineEdit(_stage.CONFIG_FIELDS[0]["default"])
        self._sn_edit.setFixedWidth(110)
        r1.addWidget(self._sn_edit)

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

        # Position / homed status row
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Position:"))
        self._pos_lbl = QLabel("—  mm")
        self._pos_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._pos_lbl.setFixedWidth(110)
        r2.addWidget(self._pos_lbl)

        r2.addWidget(QLabel("Homed:"))
        self._homed_lbl = QLabel("—")
        self._homed_lbl.setFixedWidth(30)
        r2.addWidget(self._homed_lbl)

        r2.addWidget(QLabel("Last saved:"))
        self._last_saved_lbl = QLabel("—")
        self._last_saved_lbl.setStyleSheet("color: gray;")
        r2.addWidget(self._last_saved_lbl)
        r2.addStretch()
        l.addLayout(r2)

        return grp

    def _build_presets_group(self) -> QGroupBox:
        grp = QGroupBox("Preset Positions")
        l   = QVBoxLayout(grp)

        # Editable preset values
        vals_row = QHBoxLayout()
        self._preset_spins: dict[str, QDoubleSpinBox] = {}
        for name, default in [("retrieval", 5.0), ("dropping", 6.5), ("retraction", 11.0)]:
            vals_row.addWidget(QLabel(f"{name.capitalize()}:"))
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 12.0)
            spin.setValue(default)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" mm")
            spin.setFixedWidth(110)
            self._preset_spins[name] = spin
            vals_row.addWidget(spin)
        vals_row.addStretch()
        l.addLayout(vals_row)

        # One-click preset buttons
        btn_row = QHBoxLayout()
        preset_styles = {
            "retrieval":  "#6b7280",   # grey
            "dropping":   "#16a34a",   # green
            "retraction": "#9333ea",   # purple
        }
        self._preset_btns: dict[str, QPushButton] = {}
        for name, color in preset_styles.items():
            btn = QPushButton(f"→ {name.capitalize()}")
            btn.setFixedWidth(130)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white; "
                f"font-weight: bold; padding: 5px 10px; border-radius: 4px; }}"
                f"QPushButton:hover {{ opacity: 0.85; }}"
                f"QPushButton:disabled {{ background-color: #d1d5db; color: #9ca3af; }}"
            )
            btn.setEnabled(False)
            btn.clicked.connect(lambda _, n=name: self._move_preset(n))
            self._preset_btns[name] = btn
            btn_row.addWidget(btn)
        btn_row.addStretch()
        l.addLayout(btn_row)

        return grp

    def _build_manual_group(self) -> QGroupBox:
        grp = QGroupBox("Manual Control")
        l   = QVBoxLayout(grp)

        # Home
        home_row = QHBoxLayout()
        self._home_btn = QPushButton("Home Motor")
        self._home_btn.setFixedWidth(110)
        self._home_btn.setStyleSheet(
            "QPushButton { background-color: #b45309; color: white; "
            "font-weight: bold; padding: 4px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #d97706; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._home_btn.setEnabled(False)
        self._home_btn.clicked.connect(self._on_home)
        home_row.addWidget(self._home_btn)
        home_row.addWidget(QLabel(
            "Required after power-on.  Drives to limit switch and resets encoder."))
        home_row.addStretch()
        l.addLayout(home_row)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.HLine)
        _sep.setStyleSheet("color: #e5e7eb;")
        l.addWidget(_sep)

        # Absolute move
        move_row = QHBoxLayout()
        move_row.addWidget(QLabel("Move to:"))
        self._moveto_spin = QDoubleSpinBox()
        self._moveto_spin.setRange(0.0, 12.0)
        self._moveto_spin.setValue(0.0)
        self._moveto_spin.setDecimals(3)
        self._moveto_spin.setSingleStep(0.1)
        self._moveto_spin.setSuffix(" mm")
        self._moveto_spin.setFixedWidth(110)
        move_row.addWidget(self._moveto_spin)
        self._moveto_btn = QPushButton("Move")
        self._moveto_btn.setFixedWidth(70)
        self._moveto_btn.setEnabled(False)
        self._moveto_btn.clicked.connect(self._on_move_to)
        move_row.addWidget(self._moveto_btn)
        move_row.addStretch()
        l.addLayout(move_row)

        # Jog
        jog_row = QHBoxLayout()
        self._jog_rev_btn = QPushButton("◄ Reverse")
        self._jog_rev_btn.setFixedWidth(90)
        self._jog_rev_btn.setEnabled(False)
        self._jog_rev_btn.clicked.connect(lambda: self._on_jog("reverse"))
        jog_row.addWidget(self._jog_rev_btn)
        jog_row.addWidget(QLabel("Jog step:"))
        self._jog_spin = QDoubleSpinBox()
        self._jog_spin.setRange(0.001, 2.0)
        self._jog_spin.setValue(0.1)
        self._jog_spin.setDecimals(3)
        self._jog_spin.setSingleStep(0.05)
        self._jog_spin.setSuffix(" mm")
        self._jog_spin.setFixedWidth(100)
        jog_row.addWidget(self._jog_spin)
        self._jog_fwd_btn = QPushButton("Forward ►")
        self._jog_fwd_btn.setFixedWidth(90)
        self._jog_fwd_btn.setEnabled(False)
        self._jog_fwd_btn.clicked.connect(lambda: self._on_jog("forward"))
        jog_row.addWidget(self._jog_fwd_btn)
        jog_row.addStretch()
        l.addLayout(jog_row)

        return grp

    def _build_motion_params_group(self) -> QGroupBox:
        grp = QGroupBox("Motion Parameters  (0 = keep device default)")
        grp.setCheckable(True)
        grp.setChecked(False)   # collapsed by default
        l   = QHBoxLayout(grp)

        for label, attr, default in [
            ("Velocity (mm/s)",      "_vel_spin",  1.0),
            ("Accel (mm/s²)",        "_acc_spin",  1.0),
            ("Backlash (mm)",        "_blsh_spin", 0.0),
        ]:
            l.addWidget(QLabel(f"{label}:"))
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 20.0)
            spin.setValue(default)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setFixedWidth(90)
            setattr(self, attr, spin)
            l.addWidget(spin)
        l.addStretch()

        return grp

    def _build_signal_group(self) -> QGroupBox:
        grp = QGroupBox("532 nm Alignment Signal  (from FPGA)")
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

        self._align_indicator = QLabel("FPGA not connected")
        self._align_indicator.setAlignment(Qt.AlignCenter)
        self._align_indicator.setFixedWidth(180)
        self._align_indicator.setStyleSheet(
            "background: #e5e7eb; color: #6b7280; padding: 3px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 11px;"
        )
        val_row.addWidget(self._align_indicator)
        val_row.addStretch()
        l.addLayout(val_row)

        hint = QLabel(
            "Retracted → strong signal  |  "
            "Chassis blocking → ~0  |  "
            "Dropping position → attenuated"
        )
        hint.setStyleSheet("color: gray; font-size: 10px;")
        l.addWidget(hint)

        return grp

    def _build_log_group(self) -> QGroupBox:
        grp = QGroupBox("Log")
        l   = QVBoxLayout(grp)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(160)
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
        """Assemble the module config dict from the current widget values."""
        return {
            "serial_number":      self._sn_edit.text().strip(),
            "retrieval_mm":       self._preset_spins["retrieval"].value(),
            "dropping_mm":        self._preset_spins["dropping"].value(),
            "retraction_mm":      self._preset_spins["retraction"].value(),
            "velocity_mm_s":      self._vel_spin.value(),
            "acceleration_mm_s2": self._acc_spin.value(),
            "jog_step_mm":        self._jog_spin.value(),
            "backlash_mm":        self._blsh_spin.value(),
        }

    def get_ui_state(self) -> dict:
        """Return all widget values for unified session state persistence."""
        return self._get_config()

    def restore_ui_state(self, state: dict) -> None:
        """Restore widget values from a unified session state dict."""
        def _s(attr, key, cast=float):
            if key in state:
                try:
                    getattr(self, attr).setValue(cast(state[key]))
                except Exception:
                    pass
        if "serial_number" in state:
            self._sn_edit.setText(str(state["serial_number"]))
        _s("_preset_spins", "retrieval_mm")   # handled below
        for name, key in [("retrieval", "retrieval_mm"),
                          ("dropping",  "dropping_mm"),
                          ("retraction","retraction_mm")]:
            if key in state:
                try:
                    self._preset_spins[name].setValue(float(state[key]))
                except Exception:
                    pass
        _s("_vel_spin",  "velocity_mm_s")
        _s("_acc_spin",  "acceleration_mm_s2")
        _s("_jog_spin",  "jog_step_mm")
        _s("_blsh_spin", "backlash_mm")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        """Enable or disable all command buttons."""
        for btn in self._preset_btns.values():
            btn.setEnabled(enabled and self._connected)
        for btn in (self._home_btn, self._moveto_btn,
                    self._jog_fwd_btn, self._jog_rev_btn):
            btn.setEnabled(enabled and self._connected)

    def _restore_last_position(self) -> None:
        """Show last saved position from state file on widget startup."""
        last = _stage.get_last_position()
        if last is not None:
            self._last_saved_lbl.setText(f"{last:.4f} mm")
            self._moveto_spin.setValue(last)

    def _update_position_display(self, pos_mm: float, homed: bool | None = None) -> None:
        self._position_mm = pos_mm
        self._pos_lbl.setText(f"{pos_mm:.4f} mm")
        if homed is not None:
            self._is_homed = homed
        self._homed_lbl.setText("✓" if self._is_homed else "✗")
        self._homed_lbl.setStyleSheet(
            "color: green;" if self._is_homed else "color: red;")
        self._last_saved_lbl.setText(f"{pos_mm:.4f} mm")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _on_connect(self) -> None:
        self._connect_btn.setEnabled(False)
        self._conn_status.setText("Connecting…")
        self._conn_status.setStyleSheet("color: gray;")
        self._log(f"Connecting to S/N {self._sn_edit.text().strip()} …")

        config = self._get_config()
        worker = _ReadWorker(config)
        worker.finished.connect(self._on_connect_done)
        worker.start()
        self._worker = worker

    def _on_connect_done(self, ok: bool, msg: str) -> None:
        self._worker = None
        if ok:
            self._connected = True
            self._conn_status.setText(msg)
            self._conn_status.setStyleSheet("color: green; font-weight: bold;")
            self._disconnect_btn.setEnabled(True)
            self._set_buttons_enabled(True)
            self._log(f"Connected: {msg}")

            # Parse position from the test message if possible
            try:
                for part in msg.split("|"):
                    if "position" in part:
                        pos = float(part.split(":")[1].strip().split()[0])
                        homed = "homed: yes" in msg.lower()
                        self._update_position_display(pos, homed)
                        break
            except Exception:
                pass
        else:
            self._connected = False
            self._conn_status.setText(f"Failed: {msg}")
            self._conn_status.setStyleSheet("color: red;")
            self._connect_btn.setEnabled(True)
            self._log(f"Connection failed: {msg}")

    def _on_disconnect(self) -> None:
        self._connected = False
        self._set_buttons_enabled(False)
        self._disconnect_btn.setEnabled(False)
        self._connect_btn.setEnabled(True)
        self._conn_status.setText("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        self._pos_lbl.setText("—  mm")
        self._homed_lbl.setText("—")
        self._log("Disconnected.")

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _run_command(self, action: str, **kwargs) -> None:
        """Start a command worker. Disables buttons until complete."""
        if self._worker is not None and self._worker.isRunning():
            self._log("A move is already in progress.")
            return
        self._set_buttons_enabled(False)
        config = self._get_config()
        worker = _CommandWorker(config, action, **kwargs)
        worker.finished.connect(self._on_command_done)
        worker.start()
        self._worker = worker

    def _on_command_done(self, ok: bool, msg: str, pos_mm: float) -> None:
        self._worker = None
        if ok and pos_mm > 0:
            self._update_position_display(pos_mm)
        self._log(f"{'OK' if ok else 'FAILED'}: {msg}")
        self._set_buttons_enabled(True)

    def _on_home(self) -> None:
        self._log("Homing motor…")
        self._run_command("home")

    def _on_move_to(self) -> None:
        target = self._moveto_spin.value()
        self._log(f"Moving to {target:.4f} mm…")
        self._run_command("move_to", position_mm=target)

    def _move_preset(self, preset: str) -> None:
        target = self._preset_spins[preset].value()
        self._log(f"Moving to preset '{preset}' ({target:.4f} mm)…")
        self._run_command("move_to_preset", preset=preset)

    def move_preset_public(self, preset: str) -> None:
        """Move to a named preset — callable from other panels (e.g. trapping tab)."""
        self._move_preset(preset)

    def _on_jog(self, direction: str) -> None:
        step = self._jog_spin.value()
        self._log(f"Jog {direction} {step:.3f} mm…")
        self._run_command("jog", direction=direction, step_mm=step)

    # ------------------------------------------------------------------
    # FPGA update — 532 nm signal
    # ------------------------------------------------------------------

    def on_fpga_update(self, state: dict[str, float]) -> None:
        """Called each FPGA monitor cycle. Updates 532 nm alignment display."""
        x = state.get("AI X plot", None)
        y = state.get("AI Y plot", None)

        if x is not None:
            self._xbpd_lbl.setText(f"{x:+.4f}")
        if y is not None:
            self._ybpd_lbl.setText(f"{y:+.4f}")

        if x is not None and y is not None:
            amplitude = abs(x) + abs(y)
            # Thresholds are approximate — operator should confirm these
            # in practice and adjust as needed
            if amplitude < 0.05:
                text, bg, fg = "BLOCKED", "#fef3c7", "#92400e"
            elif amplitude > 1.5:
                text, bg, fg = "RETRACTED", "#dbeafe", "#1e40af"
            else:
                text, bg, fg = "DROPPING POSITION", "#dcfce7", "#166534"
            self._align_indicator.setText(text)
            self._align_indicator.setStyleSheet(
                f"background: {bg}; color: {fg}; padding: 3px 8px; "
                f"border-radius: 4px; font-weight: bold; font-size: 11px;"
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)


# ---------------------------------------------------------------------------
# Procedure class
# ---------------------------------------------------------------------------

class Procedure(ControlProcedure):
    NAME        = "Dropper Stage"
    PERSISTENT  = True   # always loaded as a dedicated tab; excluded from Procedures list
    DESCRIPTION = (
        "Control the Z812 linear actuator (KDC101) for dropper positioning. "
        "Move to retrieval, dropping, or retraction presets; home; jog. "
        "Displays live 532 nm alignment signal from the FPGA."
    )

    def __init__(self):
        self._widget: DropperStageWidget | None = None

    def create_widget(self, parent=None) -> QWidget:
        self._widget = DropperStageWidget(self, parent)
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
