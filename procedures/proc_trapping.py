"""
procedures/proc_trapping.py

Trapping tab — all live analysis and automation for sphere trapping.

Sections
--------
  Dropper Stage shortcuts   — preset positions + move buttons
  Shake Dropper shortcuts   — ramp params, Start/Stop, Auto-catch checkbox
  Sphere Detection          — sliding-window RMS for X, Y, Z; veto LED
  Feedback Presets          — Prepare Trap / Start Feedback column grid
  Lower Sphere              — DC offset ramp with auto-freeze at trap centre

Data flow
---------
Fast FPGA data (AI X/Y/Z plot) arrives via on_fast_data(), which emits
_fast_data_signal (queued → main thread) → _on_fast_data_ui().  All RMS
computation and LED updates happen on the main thread with no locks needed.

The lower-sphere QTimer also fires on the main thread and reads the
pre-computed _z_mean / _z_rms / _z_detected attributes.

Instrument wiring
-----------------
Call Procedure.set_instruments(dropper_widget, shaker_widget) *before*
create_widget().  The panel registers a shake-event callback on the shaker
so the veto window is set automatically on each shake step.
"""

from __future__ import annotations

import collections
import time

import numpy as np

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from procedures.base import ControlProcedure

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRESET_FIELDS = [
    ("dg X",        "dg X",        False),
    ("dg Y",        "dg Y",        False),
    ("dg Z",        "dg Z",        False),
    ("ig Z",        " ig Z",       False),
    ("pg Z",        "pg Z",        False),
    ("DC offset Z", "DC offset Z", True),
]

_LED_OFF  = "background-color: #d1d5db; border-radius: 9px; border: 1px solid #9ca3af;"
_LED_ON   = "background-color: #22c55e; border-radius: 9px; border: 1px solid #16a34a;"
_LED_VETO = "background-color: #f59e0b; border-radius: 9px; border: 1px solid #d97706;"


def _led() -> QLabel:
    w = QLabel()
    w.setFixedSize(18, 18)
    w.setStyleSheet(_LED_OFF)
    return w


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class TrappingPanel(QWidget):
    """Embedded in the Trapping tab.  Receives fast FPGA data directly."""

    _fast_data_signal = pyqtSignal(dict)

    def __init__(self, fpga, parent=None):
        super().__init__(parent)
        self._fpga = fpga

        # Instrument refs — set via set_instruments() before use
        self._dropper_widget = None
        self._shaker_widget  = None

        # Sliding-window buffers: deque of (timestamp, value)
        self._x_buf: collections.deque = collections.deque()
        self._y_buf: collections.deque = collections.deque()
        self._z_buf: collections.deque = collections.deque()

        # Pre-computed values (written by _on_fast_data_ui, read by lower timer)
        self._z_mean: float = 0.0
        self._z_rms:  float = 0.0

        # Detection state
        self._x_detected: bool = False
        self._y_detected: bool = False
        self._z_detected: bool = False

        # Veto state
        self._shaking:    bool  = False  # True from "start" callback to "done" callback
        self._veto_until: float = 0.0   # monotonic time for post-shake tail

        # Lower-sphere state
        self._lowering:   bool  = False
        self._current_dc: float = 0.0

        # Preset spinbox registry
        self._preset_spins: dict = {"prepare": {}, "feedback": {}}
        self._preset_regs:  dict = {name: reg for name, reg, _ in PRESET_FIELDS}
        self._preset_readback_lbls: dict = {}   # field → col-3 "Current FPGA" label
        self._dc_offset_fb_lbl = None           # col-2 DC offset Z label (no spinbox)
        self._z_setpoint_hint_lbl = None        # gray hint next to Z setpoint spinbox
        self._lower_dc_offset_lbl = None        # DC offset readback in lower-sphere row

        # Lower-sphere timer (main thread)
        self._lower_timer = QTimer(self)
        self._lower_timer.timeout.connect(self._on_lower_timer)

        self._build_ui()
        self._fast_data_signal.connect(self._on_fast_data_ui)

    # ------------------------------------------------------------------
    # Instrument wiring (called from Procedure.set_instruments)
    # ------------------------------------------------------------------

    def set_instruments(self, dropper_widget, shaker_widget) -> None:
        self._dropper_widget = dropper_widget
        self._shaker_widget  = shaker_widget
        shaker_widget.set_shake_event_callback(self._on_shake_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_fast_data(self, values: dict) -> None:
        """Thread-safe: call from any thread (e.g. monitor thread via fpga_gui)."""
        self._fast_data_signal.emit(values)

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
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._build_dropper_group())
        layout.addWidget(self._build_shaker_group())
        layout.addWidget(self._build_detection_group())
        layout.addWidget(self._build_presets_group())
        layout.addWidget(self._build_lower_sphere_group())
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ---- Dropper shortcuts ----

    def _build_dropper_group(self) -> QGroupBox:
        grp = QGroupBox(
            "Dropper Stage  (shortcut — connect on Dropper Stage tab first)")
        gl = QVBoxLayout(grp)

        vals_row = QHBoxLayout()
        self._dropper_spins: dict[str, QDoubleSpinBox] = {}
        for name, default in [
                ("Retrieval", 5.0), ("Dropping", 6.5), ("Retraction", 11.0)]:
            vals_row.addWidget(QLabel(f"{name}:"))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 12.0)
            sp.setValue(default)
            sp.setDecimals(3)
            sp.setSuffix(" mm")
            sp.setFixedWidth(100)
            self._dropper_spins[name.lower()] = sp
            vals_row.addWidget(sp)
        vals_row.addStretch()
        gl.addLayout(vals_row)

        btn_row = QHBoxLayout()
        _colors = {
            "retrieval":  "#6b7280",
            "dropping":   "#16a34a",
            "retraction": "#9333ea",
        }
        for name, color in _colors.items():
            btn = QPushButton(f"→ {name.capitalize()}")
            btn.setFixedWidth(130)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white; "
                f"font-weight: bold; padding: 5px 10px; border-radius: 4px; }}"
                f"QPushButton:hover {{ opacity: 0.85; }}"
                f"QPushButton:disabled {{ background-color: #d1d5db; color: #9ca3af; }}"
            )
            btn.clicked.connect(lambda _, n=name: self._move_preset(n))
            btn_row.addWidget(btn)
        btn_row.addStretch()
        gl.addLayout(btn_row)

        return grp

    # ---- Shaker shortcuts ----

    def _build_shaker_group(self) -> QGroupBox:
        grp = QGroupBox(
            "Shake Dropper  (shortcut — connect AWG + PSU on Shake Dropper tab first)")
        gl = QVBoxLayout(grp)

        params_row = QHBoxLayout()
        _params = [
            ("Start V:",  "_sk_start_v", 5.0,  " V",  0.0,  60.0,  0.5, 2, False),
            ("Step:",     "_sk_step_v",  2.0,  " V",  0.01, 30.0,  0.5, 2, False),
            ("Steps:",    "_sk_steps",   10,   "",    1,    500,   1,   0, True),
            ("Dwell:",    "_sk_dwell",   5.0,  " s",  0.1,  120.0, 0.5, 1, False),
            ("Max V:",    "_sk_max_v",   60.0, " V",  0.0,  60.0,  1.0, 1, False),
        ]
        for label, attr, default, suffix, lo, hi, step, dec, is_int in _params:
            params_row.addWidget(QLabel(label))
            if is_int:
                sp = QSpinBox()
                sp.setRange(int(lo), int(hi))
                sp.setValue(int(default))
                sp.setFixedWidth(65)
            else:
                sp = QDoubleSpinBox()
                sp.setRange(lo, hi)
                sp.setValue(default)
                sp.setDecimals(dec)
                sp.setSingleStep(step)
                sp.setSuffix(suffix)
                sp.setFixedWidth(90)
            setattr(self, attr, sp)
            params_row.addWidget(sp)
        params_row.addStretch()
        gl.addLayout(params_row)

        ctrl_row = QHBoxLayout()
        self._start_shake_btn = QPushButton("▶  Start Shaking")
        self._start_shake_btn.setFixedHeight(34)
        self._start_shake_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #22c55e; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._start_shake_btn.clicked.connect(self._on_start_shaking)
        ctrl_row.addWidget(self._start_shake_btn)

        self._stop_shake_btn = QPushButton("■  Stop")
        self._stop_shake_btn.setFixedHeight(34)
        self._stop_shake_btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )
        self._stop_shake_btn.clicked.connect(self._on_stop_shaking)
        ctrl_row.addWidget(self._stop_shake_btn)

        ctrl_row.addSpacing(20)
        self._auto_catch_check = QCheckBox(
            "Auto-catch  (stop shaking when both X + Y detected)")
        ctrl_row.addWidget(self._auto_catch_check)
        ctrl_row.addStretch()
        gl.addLayout(ctrl_row)

        return grp

    # ---- Sphere detection ----

    def _build_detection_group(self) -> QGroupBox:
        grp = QGroupBox("Sphere Detection")
        gl = QVBoxLayout(grp)

        def _ch_row(axis: str, default_thresh: float, decimals: int,
                    thresh_width: int = 90):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{axis} RMS thresh:"))
            thresh = QDoubleSpinBox()
            thresh.setRange(0.0, 1e7)
            thresh.setValue(default_thresh)
            thresh.setDecimals(decimals)
            thresh.setFixedWidth(thresh_width)
            row.addWidget(thresh)
            row.addSpacing(6)
            row.addWidget(QLabel("Current:"))
            rms_lbl = QLabel("—")
            rms_lbl.setFixedWidth(80)
            rms_lbl.setStyleSheet("color: #6b7280;")
            row.addWidget(rms_lbl)
            led = _led()
            row.addWidget(led)
            row.addWidget(QLabel(f"{axis} detected"))
            row.addStretch()
            gl.addLayout(row)
            return thresh, rms_lbl, led

        self._x_thresh_spin, self._x_rms_lbl, self._x_led = _ch_row("X", 0.05, 4)
        self._y_thresh_spin, self._y_rms_lbl, self._y_led = _ch_row("Y", 0.05, 4)
        self._z_thresh_spin, self._z_rms_lbl, self._z_led = _ch_row("Z", 100.0, 1)

        # Veto + window settings
        veto_row = QHBoxLayout()
        self._veto_led = _led()
        veto_row.addWidget(self._veto_led)
        vl = QLabel("Veto active")
        vl.setStyleSheet("color: #d97706; font-weight: bold;")
        veto_row.addWidget(vl)
        veto_row.addSpacing(16)

        for label, attr, default in [
            ("Post-shake veto:", "_post_veto_spin", 1.0),
            ("Window:",          "_win_spin",        1.0),
        ]:
            veto_row.addWidget(QLabel(label))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 60.0)
            sp.setValue(default)
            sp.setDecimals(1)
            sp.setSuffix(" s")
            sp.setFixedWidth(70)
            setattr(self, attr, sp)
            veto_row.addWidget(sp)
            veto_row.addSpacing(4)

        veto_row.addStretch()
        gl.addLayout(veto_row)

        return grp

    # ---- Feedback presets ----

    def _build_presets_group(self) -> QGroupBox:
        grp = QGroupBox("Feedback Presets")
        pg = QGridLayout()
        pg.setSpacing(5)
        pg.setColumnStretch(0, 1)

        for col, text in [(1, "Prepare Trap"), (2, "Start Feedback"), (3, "Current FPGA")]:
            hdr = QLabel(f"<b>{text}</b>")
            hdr.setAlignment(Qt.AlignCenter)
            pg.addWidget(hdr, 0, col)

        for row, (field, _reg, is_int) in enumerate(PRESET_FIELDS, start=1):
            pg.addWidget(QLabel(field), row, 0)

            # Prepare Trap column — always a spinbox
            sp_prep = QDoubleSpinBox()
            sp_prep.setRange(-1e7, 1e7)
            if is_int:
                sp_prep.setDecimals(0)
                sp_prep.setSingleStep(1)
            else:
                sp_prep.setDecimals(6)
                sp_prep.setSingleStep(0.0001)
            sp_prep.setValue(0.0)
            sp_prep.setFixedWidth(120)
            self._preset_spins["prepare"][field] = sp_prep
            pg.addWidget(sp_prep, row, 1)

            # Start Feedback column — DC offset Z is read-only (set by Lower Sphere)
            if field == "DC offset Z":
                dc_lbl = QLabel("—")
                dc_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                dc_lbl.setStyleSheet("color: #6b7280; font-style: italic;")
                dc_lbl.setFixedWidth(120)
                dc_lbl.setToolTip("Set automatically by Lower Sphere")
                self._dc_offset_fb_lbl = dc_lbl
                pg.addWidget(dc_lbl, row, 2)
            else:
                sp_fb = QDoubleSpinBox()
                sp_fb.setRange(-1e7, 1e7)
                if is_int:
                    sp_fb.setDecimals(0)
                    sp_fb.setSingleStep(1)
                else:
                    sp_fb.setDecimals(6)
                    sp_fb.setSingleStep(0.0001)
                sp_fb.setValue(0.0)
                sp_fb.setFixedWidth(120)
                self._preset_spins["feedback"][field] = sp_fb
                pg.addWidget(sp_fb, row, 2)

            # Current FPGA column — gray readback for all rows
            rb_lbl = QLabel("—")
            rb_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rb_lbl.setStyleSheet("color: #6b7280;")
            rb_lbl.setFixedWidth(120)
            self._preset_readback_lbls[field] = rb_lbl
            pg.addWidget(rb_lbl, row, 3)

        brow = len(PRESET_FIELDS) + 1
        arow = brow
        prep_btn = QPushButton("Prepare Trap")
        prep_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px; "
            "background-color: #7c3aed; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #9333ea; }"
        )
        prep_btn.clicked.connect(lambda: self._write_preset("prepare"))
        pg.addWidget(prep_btn, arow, 1)

        fb_btn = QPushButton("Start Feedback")
        fb_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px; "
            "background-color: #2563eb; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        fb_btn.clicked.connect(lambda: self._write_preset("feedback"))
        pg.addWidget(fb_btn, arow, 2)

        grp.setLayout(pg)
        return grp

    # ---- Lower sphere ----

    def _build_lower_sphere_group(self) -> QGroupBox:
        grp = QGroupBox("Lower Sphere")
        lg = QVBoxLayout(grp)

        c1 = QHBoxLayout()
        c1.addWidget(QLabel("DAC/s:"))
        self._dac_per_s_spin = QDoubleSpinBox()
        self._dac_per_s_spin.setRange(0.1, 1e6)
        self._dac_per_s_spin.setValue(100.0)
        self._dac_per_s_spin.setDecimals(1)
        self._dac_per_s_spin.setFixedWidth(90)
        c1.addWidget(self._dac_per_s_spin)
        c1.addSpacing(8)

        c1.addWidget(QLabel("Lower limit (cts):"))
        self._lower_limit_spin = QSpinBox()
        self._lower_limit_spin.setRange(-32768, 32767)
        self._lower_limit_spin.setValue(-2000)
        self._lower_limit_spin.setFixedWidth(90)
        c1.addWidget(self._lower_limit_spin)
        c1.addSpacing(8)

        c1.addWidget(QLabel("Z setpoint (cts):"))
        self._z_setpoint_spin = QSpinBox()
        self._z_setpoint_spin.setRange(-32768, 32767)
        self._z_setpoint_spin.setValue(0)
        self._z_setpoint_spin.setFixedWidth(90)
        c1.addWidget(self._z_setpoint_spin)
        self._z_setpoint_hint_lbl = QLabel("(avg: —)")
        self._z_setpoint_hint_lbl.setStyleSheet("color: #6b7280; font-size: 10px;")
        self._z_setpoint_hint_lbl.setToolTip(
            "Current mean Z — auto-loaded as setpoint when Lower Sphere is clicked")
        c1.addWidget(self._z_setpoint_hint_lbl)
        c1.addSpacing(8)

        c1.addWidget(QLabel("Tol (±cts):"))
        self._z_tol_spin = QSpinBox()
        self._z_tol_spin.setRange(1, 100000)
        self._z_tol_spin.setValue(50)
        self._z_tol_spin.setFixedWidth(75)
        c1.addWidget(self._z_tol_spin)
        c1.addSpacing(8)

        c1.addWidget(QLabel("Update:"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(10, 60000)
        self._interval_spin.setValue(100)
        self._interval_spin.setSuffix(" ms")
        self._interval_spin.setFixedWidth(80)
        c1.addWidget(self._interval_spin)
        c1.addSpacing(8)

        c1.addWidget(QLabel("DC Z:"))
        self._lower_dc_offset_lbl = QLabel("—")
        self._lower_dc_offset_lbl.setFixedWidth(65)
        self._lower_dc_offset_lbl.setStyleSheet("color: #6b7280;")
        self._lower_dc_offset_lbl.setToolTip("Current DC offset Z (cts)")
        c1.addWidget(self._lower_dc_offset_lbl)

        c1.addStretch()
        lg.addLayout(c1)

        bi = QHBoxLayout()
        lower_btn = QPushButton("Lower Sphere")
        lower_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 12px; "
            "background-color: #d97706; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #f59e0b; }"
        )
        lower_btn.clicked.connect(self._on_lower_sphere)
        bi.addWidget(lower_btn)

        stop_btn = QPushButton("Stop")
        stop_btn.setStyleSheet(
            "QPushButton { color: #b91c1c; padding: 5px 10px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #fee2e2; }"
        )
        stop_btn.clicked.connect(self._on_stop_lower)
        bi.addWidget(stop_btn)
        bi.addSpacing(20)

        bi.addWidget(QLabel("Avg Z:"))
        self._avg_z_lbl = QLabel("—")
        self._avg_z_lbl.setFixedWidth(75)
        self._avg_z_lbl.setStyleSheet("color: #6b7280; font-size: 10px;")
        bi.addWidget(self._avg_z_lbl)
        bi.addSpacing(8)

        bi.addWidget(QLabel("RMS Z:"))
        self._rms_z_disp_lbl = QLabel("—")
        self._rms_z_disp_lbl.setFixedWidth(75)
        self._rms_z_disp_lbl.setStyleSheet("color: #6b7280; font-size: 10px;")
        bi.addWidget(self._rms_z_disp_lbl)
        bi.addSpacing(12)

        # Separate LED for "sphere at trap centre" (distinct from Z detected LED)
        self._center_led = _led()
        bi.addWidget(self._center_led)
        self._center_lbl = QLabel("Sphere at centre")
        self._center_lbl.setStyleSheet("color: #16a34a; font-weight: bold;")
        self._center_lbl.setVisible(False)
        bi.addWidget(self._center_lbl)
        bi.addStretch()

        lg.addLayout(bi)
        grp.setLayout(lg)
        return grp

    # ------------------------------------------------------------------
    # Fast data handling (main thread via queued signal)
    # ------------------------------------------------------------------

    def _on_fast_data_ui(self, values: dict) -> None:
        t      = time.monotonic()
        in_veto = self._shaking or t < self._veto_until
        win_s  = self._win_spin.value()
        cutoff = t - win_s

        # Veto LED
        self._veto_led.setStyleSheet(_LED_VETO if in_veto else _LED_OFF)

        # Z — always buffered (veto doesn't apply: sphere may be in trap
        # at any height while the dropper is shaking above it)
        if "AI Z plot" in values:
            self._z_buf.append((t, float(values["AI Z plot"])))
        while self._z_buf and self._z_buf[0][0] < cutoff:
            self._z_buf.popleft()

        # X/Y — skip during veto window to exclude shake pulse artefacts
        if not in_veto:
            if "AI X plot" in values:
                self._x_buf.append((t, float(values["AI X plot"])))
            if "AI Y plot" in values:
                self._y_buf.append((t, float(values["AI Y plot"])))
        while self._x_buf and self._x_buf[0][0] < cutoff:
            self._x_buf.popleft()
        while self._y_buf and self._y_buf[0][0] < cutoff:
            self._y_buf.popleft()

        # RMS (std removes DC component — sensitive to oscillations)
        def _rms(buf):
            if len(buf) < 2:
                return 0.0
            return float(np.std([v for _, v in buf]))

        def _mean(buf):
            if not buf:
                return 0.0
            return float(np.mean([v for _, v in buf]))

        x_rms  = _rms(self._x_buf)
        y_rms  = _rms(self._y_buf)
        z_rms  = _rms(self._z_buf)
        z_mean = _mean(self._z_buf)

        # Cache for lower timer
        self._z_mean = z_mean
        self._z_rms  = z_rms

        # Update display
        self._x_rms_lbl.setText(f"{x_rms:.4f}")
        self._y_rms_lbl.setText(f"{y_rms:.4f}")
        self._z_rms_lbl.setText(f"{z_rms:.1f}")
        self._avg_z_lbl.setText(f"{z_mean:.1f}")
        self._rms_z_disp_lbl.setText(f"{z_rms:.1f}")
        self._z_setpoint_hint_lbl.setText(f"(avg: {z_mean:.0f})")

        # Detection thresholds
        x_det = x_rms > self._x_thresh_spin.value()
        y_det = y_rms > self._y_thresh_spin.value()
        z_det = z_rms > self._z_thresh_spin.value()

        if x_det != self._x_detected:
            self._x_detected = x_det
            self._x_led.setStyleSheet(_LED_ON if x_det else _LED_OFF)

        if y_det != self._y_detected:
            self._y_detected = y_det
            self._y_led.setStyleSheet(_LED_ON if y_det else _LED_OFF)

        if z_det != self._z_detected:
            self._z_detected = z_det
            self._z_led.setStyleSheet(_LED_ON if z_det else _LED_OFF)

        # Auto-catch: stop shaking when both X and Y are detected (outside veto)
        if (self._auto_catch_check.isChecked()
                and x_det and y_det
                and not in_veto
                and self._shaker_widget is not None):
            self._shaker_widget.request_stop()

    # ------------------------------------------------------------------
    # Shake event callback — sets veto window (called on main thread)
    # ------------------------------------------------------------------

    def _on_shake_event(self, event_type: str) -> None:
        if event_type == "start":
            # Sequence beginning — clear stale data; sweep_start will arm the veto
            self._x_buf.clear()
            self._y_buf.clear()
        elif event_type == "sweep_start":
            # AWG about to fire — veto active for this sweep
            self._shaking = True
        elif event_type == "step":
            # Sweep done, dwell beginning — clear sweep veto, start post-veto settling
            self._shaking = False
            self._veto_until = time.monotonic() + self._post_veto_spin.value()
        elif event_type == "done":
            # Full sequence finished — final post-veto settling window
            self._shaking = False
            self._veto_until = time.monotonic() + self._post_veto_spin.value()

    # ------------------------------------------------------------------
    # Dropper shortcuts
    # ------------------------------------------------------------------

    def _move_preset(self, name: str) -> None:
        if self._dropper_widget is None:
            return
        # Push the local preset value into the dropper tab's spinbox, then move
        val = self._dropper_spins[name].value()
        dw  = self._dropper_widget
        if name in dw._preset_spins:
            dw._preset_spins[name].setValue(val)
        dw.move_preset_public(name)

    # ------------------------------------------------------------------
    # Shaker shortcuts
    # ------------------------------------------------------------------

    def _on_start_shaking(self) -> None:
        if self._shaker_widget is None:
            return
        self._shaker_widget.start_shaking_public(
            start_v = self._sk_start_v.value(),
            step_v  = self._sk_step_v.value(),
            n_steps = self._sk_steps.value(),
            dwell_s = self._sk_dwell.value(),
            max_v   = self._sk_max_v.value(),
        )

    def _on_stop_shaking(self) -> None:
        if self._shaker_widget is not None:
            self._shaker_widget.request_stop()

    # ------------------------------------------------------------------
    # Lower sphere
    # ------------------------------------------------------------------

    def _on_lower_sphere(self) -> None:
        if self._fpga is None or not self._fpga.is_connected:
            return
        try:
            self._current_dc = float(self._fpga.read_register("DC offset Z"))
        except Exception:
            self._current_dc = 0.0

        # Auto-set Z setpoint from current mean Z in the sliding window buffer
        if self._z_buf:
            self._z_setpoint_spin.setValue(int(round(self._z_mean)))

        self._lower_dc_offset_lbl.setText(f"{self._current_dc:.0f}")
        self._lowering = True
        self._center_lbl.setVisible(False)
        self._center_led.setStyleSheet(_LED_OFF)
        self._lower_timer.start(self._interval_spin.value())

    def _on_stop_lower(self) -> None:
        self._lowering = False
        self._lower_timer.stop()

    def _on_lower_timer(self) -> None:
        mean_z = self._z_mean
        rms_z  = self._z_rms

        if not self._lowering:
            return

        z_setpoint = self._z_setpoint_spin.value()
        tolerance  = self._z_tol_spin.value()

        # Freeze: Z signal detected AND mean near setpoint
        if self._z_detected and abs(mean_z - z_setpoint) <= tolerance:
            self._lowering = False
            self._lower_timer.stop()
            # Write the final frozen value explicitly so FPGA holds the settled position
            try:
                if self._fpga is not None and self._fpga.is_connected:
                    self._fpga.write_register("DC offset Z", round(self._current_dc))
            except Exception:
                pass
            self._center_led.setStyleSheet(_LED_ON)
            self._center_lbl.setVisible(True)
            return

        # Step DC offset downward
        interval_ms = self._interval_spin.value()
        delta       = self._dac_per_s_spin.value() * (interval_ms / 1000.0)
        new_dc      = self._current_dc - delta
        lower_limit = float(self._lower_limit_spin.value())

        if new_dc <= lower_limit:
            new_dc = lower_limit
            self._lowering = False
            self._lower_timer.stop()

        self._current_dc = new_dc
        self._lower_dc_offset_lbl.setText(f"{self._current_dc:.0f}")
        try:
            if self._fpga is not None and self._fpga.is_connected:
                self._fpga.write_register("DC offset Z", round(new_dc))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Feedback presets
    # ------------------------------------------------------------------

    def _write_preset(self, key: str) -> None:
        if self._fpga is None or not self._fpga.is_connected:
            return
        for field, reg in self._preset_regs.items():
            if field not in self._preset_spins[key]:
                continue  # DC offset Z not in "feedback" spinboxes — set by Lower Sphere
            val = self._preset_spins[key][field].value()
            try:
                self._fpga.write_register(reg, val)
            except Exception:
                pass

    def update_preset_readbacks(self, state: dict) -> None:
        """Update gray readback labels in the Feedback Presets grid and lower-sphere
        section from an FPGA register snapshot (slow poll, ~5 Hz)."""
        for field, reg, is_int in PRESET_FIELDS:
            val = state.get(reg)
            if val is None:
                continue
            text = f"{int(round(val))}" if is_int else f"{val:.4f}"
            lbl = self._preset_readback_lbls.get(field)
            if lbl is not None:
                lbl.setText(text)
            # DC offset Z col-2 label (no spinbox) mirrors the FPGA readback
            if field == "DC offset Z" and self._dc_offset_fb_lbl is not None:
                self._dc_offset_fb_lbl.setText(text)
            # Lower-sphere DC offset readback — only when not actively lowering
            if field == "DC offset Z" and not self._lowering:
                self._lower_dc_offset_lbl.setText(text)

    def get_ui_state(self) -> dict:
        """Return all widget values for unified session state persistence."""
        return {
            "prepare": {field: sp.value() for field, sp in self._preset_spins["prepare"].items()},
            "feedback": {field: sp.value() for field, sp in self._preset_spins["feedback"].items()},
            "lower_sphere": {
                "dac_per_s":   self._dac_per_s_spin.value(),
                "lower_limit": self._lower_limit_spin.value(),
                "z_setpoint":  self._z_setpoint_spin.value(),
                "tolerance":   self._z_tol_spin.value(),
                "interval_ms": self._interval_spin.value(),
            },
            "detection": {
                "x_thresh":  self._x_thresh_spin.value(),
                "y_thresh":  self._y_thresh_spin.value(),
                "z_thresh":  self._z_thresh_spin.value(),
                "post_veto": self._post_veto_spin.value(),
                "window_s":  self._win_spin.value(),
            },
            "dropper_shortcuts": {n: sp.value() for n, sp in self._dropper_spins.items()},
            "shaker_shortcuts": {
                "start_v": self._sk_start_v.value(),
                "step_v":  self._sk_step_v.value(),
                "n_steps": self._sk_steps.value(),
                "dwell_s": self._sk_dwell.value(),
                "max_v":   self._sk_max_v.value(),
            },
        }

    def restore_ui_state(self, state: dict) -> None:
        """Restore all widget values from a unified session state dict."""
        for key, spins in self._preset_spins.items():
            if key in state:
                for field, sp in spins.items():
                    if field in state[key]:
                        try:
                            sp.setValue(float(state[key][field]))
                        except Exception:
                            pass

        def _set(attr, d, k):
            if k in d:
                try:
                    getattr(self, attr).setValue(float(d[k]))
                except Exception:
                    pass

        ls = state.get("lower_sphere", {})
        _set("_dac_per_s_spin",   ls, "dac_per_s")
        _set("_lower_limit_spin", ls, "lower_limit")
        _set("_z_setpoint_spin",  ls, "z_setpoint")
        _set("_z_tol_spin",       ls, "tolerance")
        _set("_interval_spin",    ls, "interval_ms")

        det = state.get("detection", {})
        _set("_x_thresh_spin",  det, "x_thresh")
        _set("_y_thresh_spin",  det, "y_thresh")
        _set("_z_thresh_spin",  det, "z_thresh")
        _set("_post_veto_spin", det, "post_veto")
        _set("_win_spin",       det, "window_s")

        drp = state.get("dropper_shortcuts", {})
        for n, sp in self._dropper_spins.items():
            if n in drp:
                try:
                    sp.setValue(float(drp[n]))
                except Exception:
                    pass

        sk = state.get("shaker_shortcuts", {})
        _set("_sk_start_v", sk, "start_v")
        _set("_sk_step_v",  sk, "step_v")
        _set("_sk_steps",   sk, "n_steps")
        _set("_sk_dwell",   sk, "dwell_s")
        _set("_sk_max_v",   sk, "max_v")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        self._lower_timer.stop()


# ---------------------------------------------------------------------------
# Procedure class
# ---------------------------------------------------------------------------

class Procedure(ControlProcedure):
    NAME       = "Trapping"
    PERSISTENT = True   # always loaded as a dedicated tab; excluded from Procedures list
    DESCRIPTION = (
        "Sphere trapping workflow: dropper stage shortcuts, shake-dropper "
        "shortcuts with auto-catch, X/Y/Z RMS sphere detection with configurable "
        "veto window, feedback presets, and lower-sphere DC ramp with auto-freeze."
    )

    def __init__(self):
        self._widget: TrappingPanel | None = None
        self._dropper_widget = None
        self._shaker_widget  = None

    def set_instruments(self, dropper_widget, shaker_widget) -> None:
        """Call before create_widget() so the panel can wire instrument callbacks."""
        self._dropper_widget = dropper_widget
        self._shaker_widget  = shaker_widget
        if self._widget is not None:
            self._widget.set_instruments(dropper_widget, shaker_widget)

    def create_widget(self, parent=None) -> QWidget:
        self._widget = TrappingPanel(self.fpga, parent)
        if self._dropper_widget is not None:
            self._widget.set_instruments(self._dropper_widget, self._shaker_widget)
        return self._widget

    def on_fpga_update(self, state: dict) -> None:
        if self._widget is not None:
            self._widget.update_preset_readbacks(state)

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
