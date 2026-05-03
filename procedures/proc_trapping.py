"""
procedures/proc_trapping.py

Trapping tab — all live analysis and automation for sphere trapping.

Sections
--------
  Dropper Stage shortcuts   — preset positions + move buttons
  Shake Dropper shortcuts   — ramp params, Start/Stop, Auto-catch checkbox
  Sphere Detection          — Catching / Trapping / Lock thresholds with LEDs
  Feedback Presets          — Prepare Trap / Start Feedback column grid
  Lower Sphere              — DC offset ramp with auto-freeze at trap centre
  Trapping Macros           — Trap Sphere, Continue Trapping, Release Sphere

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

from PyQt5.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
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

_SETTINGS = QSettings("usphere", "TrappingPanel")

from procedures.base import ControlProcedure

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRESET_FIELDS = [
    ("Z Setpoint",  "Z Setpoint",  True),
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

# Macro states
_ST_IDLE       = 0
_ST_PREPARING  = 1   # move dropper → dropping, raise DC offset Z, zero gains
_ST_SHAKING    = 2   # shaker running, watching catch condition
_ST_RETRACTING = 3   # dropper moving to retraction, waiting for arrival
_ST_CHECKING   = 4   # dropper out, checking trap condition
_ST_LOWERING   = 5   # ramping DC offset Z to bring sphere to focus
_ST_FB_START   = 6   # writing feedback preset
_ST_LOCKING    = 7   # waiting N consecutive ticks below lock thresholds
_ST_LOCKED     = 8   # sphere locked — keep monitoring
_ST_EXHAUSTED  = 9   # shaker reached max voltage — dropper empty

# Each entry maps an endpoint name to the state that, when completed, stops the sequence
_ENDPOINT_NAMES  = ["Catch", "Trap check", "At focus", "Feedback start", "Lock"]
_ENDPOINT_STATES = {
    "Catch":          _ST_SHAKING,
    "Trap check":     _ST_CHECKING,
    "At focus":       _ST_LOWERING,
    "Feedback start": _ST_FB_START,
    "Lock":           _ST_LOCKING,
}

_STATE_NAMES = {
    _ST_IDLE: "Idle", _ST_PREPARING: "Preparing", _ST_SHAKING: "Shaking",
    _ST_RETRACTING: "Retracting", _ST_CHECKING: "Checking trap",
    _ST_LOWERING: "Lowering", _ST_FB_START: "Starting feedback",
    _ST_LOCKING: "Locking", _ST_LOCKED: "Locked", _ST_EXHAUSTED: "Exhausted",
}


# ---------------------------------------------------------------------------
# Signal conditions (modular — subclass _Condition to swap analysis method)
# ---------------------------------------------------------------------------

class _Condition:
    """Single-method interface.  Subclass and override check() to change how
    a state transition signal is computed (RMS, peak, bandpass filter, etc.)."""
    def check(self, panel: object) -> bool:
        return False


class _XYRMSAbove(_Condition):
    """True when X RMS > x_thresh_spinbox AND Y RMS > y_thresh_spinbox."""
    def __init__(self, x_attr: str, y_attr: str) -> None:
        self._xa, self._ya = x_attr, y_attr

    def check(self, p) -> bool:
        return (p._x_rms > getattr(p, self._xa).value() and
                p._y_rms > getattr(p, self._ya).value())


class _XYZRMSBelow(_Condition):
    """True when X, Y, Z RMS are all below their respective spinbox thresholds."""
    def __init__(self, x_attr: str, y_attr: str, z_attr: str) -> None:
        self._xa, self._ya, self._za = x_attr, y_attr, z_attr

    def check(self, p) -> bool:
        return (p._x_rms < getattr(p, self._xa).value() and
                p._y_rms < getattr(p, self._ya).value() and
                p._z_rms < getattr(p, self._za).value())


class _ZRMSAbove(_Condition):
    """True when Z RMS > z_thresh_spinbox (used for focus / sphere-present detection)."""
    def __init__(self, z_attr: str) -> None:
        self._za = z_attr

    def check(self, p) -> bool:
        return p._z_rms > getattr(p, self._za).value()


def _led() -> QLabel:
    w = QLabel()
    w.setFixedSize(18, 18)
    w.setStyleSheet(_LED_OFF)
    return w


def _hsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color: #e5e7eb;")
    return f


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

        # Pre-computed RMS / mean (written by _on_fast_data_ui, read by timers)
        self._x_rms:  float = 0.0
        self._y_rms:  float = 0.0
        self._z_mean: float = 0.0
        self._z_rms:  float = 0.0

        # Detection state — catching thresholds (during shaking)
        self._x_detected: bool = False
        self._y_detected: bool = False
        self._z_detected: bool = False

        # Detection state — trapping thresholds (after retraction; RMS above = sphere present)
        self._x_trapped: bool = False
        self._y_trapped: bool = False

        # Detection state — lock thresholds (RMS below = sphere stable)
        self._x_locked: bool = False
        self._y_locked: bool = False
        self._z_locked: bool = False

        # Veto state
        self._shaking:    bool  = False
        self._veto_until: float = 0.0

        # Lower-sphere state
        self._lowering:   bool  = False
        self._current_dc: float = 0.0

        # Macro state machine
        self._macro_state:         int   = _ST_IDLE
        self._macro_state_entry_t: float = 0.0   # monotonic time when current state was entered
        self._macro_tick_count:    int   = 0      # consecutive pass/fail counter within a state
        self._macro_stable_reads:  int   = 0      # consecutive dropper position stable reads
        self._macro_fb_retries:    int   = 0      # feedback restart attempts in LOCKING
        self._macro_fb_restarting: bool  = False  # True for one tick after stopping feedback
        self._macro_sweep_done:    bool  = False  # set by shake_event "done", consumed in tick

        # Token store — single source of truth about hardware + sphere state
        self._tokens: dict = {
            "dropper":  "unknown",   # unknown | moving | dropping | retracted
            "laser":    "unknown",   # unknown | high | low
            "feedback": "off",       # off | on
            "shaker":   "stopped",   # stopped | running | exhausted
            "sphere":   "none",      # none | caught | trapped | focused | locked
        }

        # Modular signal conditions — swap class instance to change analysis method
        self._catch_cond = _XYRMSAbove("_x_thresh_spin",      "_y_thresh_spin")
        self._trap_cond  = _XYRMSAbove("_trap_x_thresh_spin", "_trap_y_thresh_spin")
        self._focus_cond = _ZRMSAbove("_z_thresh_spin")
        self._lock_cond  = _XYZRMSBelow(
            "_lock_x_thresh_spin", "_lock_y_thresh_spin", "_lock_z_thresh_spin")

        # Preset spinbox registry
        self._preset_spins: dict = {"prepare": {}, "feedback": {}}
        self._preset_regs:  dict = {name: reg for name, reg, _ in PRESET_FIELDS}
        self._preset_readback_lbls: dict = {}
        self._dc_offset_fb_lbl     = None
        self._z_setpoint_hint_lbl  = None
        self._lower_dc_offset_lbl  = None

        # Lower-sphere timer (main thread)
        self._lower_timer = QTimer(self)
        self._lower_timer.timeout.connect(self._on_lower_timer)

        # Macro polling timer (250 ms, main thread)
        self._macro_timer = QTimer(self)
        self._macro_timer.timeout.connect(self._on_macro_tick)

        # Always-on dropper position readout (500 ms)
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._refresh_dropper_pos)
        self._pos_timer.start(500)

        self._build_ui()
        self._fast_data_signal.connect(self._on_fast_data_ui)

    # ------------------------------------------------------------------
    # Instrument wiring
    # ------------------------------------------------------------------

    def set_instruments(self, dropper_widget, shaker_widget) -> None:
        self._dropper_widget = dropper_widget
        self._shaker_widget  = shaker_widget
        shaker_widget.set_shake_event_callback(self._on_shake_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_fast_data(self, values: dict) -> None:
        """Thread-safe: call from any thread."""
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
        layout.addWidget(self._build_macro_group())
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

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Current position:"))
        self._dropper_pos_lbl = QLabel("—  mm")
        self._dropper_pos_lbl.setStyleSheet("color: #6b7280; font-style: italic;")
        self._dropper_pos_lbl.setFixedWidth(80)
        self._dropper_pos_lbl.setToolTip("Live dropper stage position (read from Dropper Stage tab)")
        pos_row.addWidget(self._dropper_pos_lbl)
        pos_row.addStretch()
        gl.addLayout(pos_row)

        btn_row = QHBoxLayout()
        _colors = {
            "retrieval":  "#6b7280",
            "dropping":   "#16a34a",
            "retraction": "#9333ea",
        }
        for name, color in _colors.items():
            btn = QPushButton(f"→ {name.capitalize()}")
            btn.setMinimumWidth(140)
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

        self._resume_shake_btn = QPushButton("↺  Resume")
        self._resume_shake_btn.setFixedHeight(34)
        self._resume_shake_btn.setStyleSheet(
            "QPushButton { background-color: #0891b2; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #06b6d4; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._resume_shake_btn.setToolTip(
            "Resume shaking from where it stopped. If the macro is idle, "
            "also re-enters the catch-detection watch state.")
        self._resume_shake_btn.clicked.connect(self._on_resume_shaking)
        ctrl_row.addWidget(self._resume_shake_btn)

        ctrl_row.addSpacing(20)
        self._auto_catch_check = QCheckBox(
            "Auto-catch  (stop shaking when both X + Y detected)")
        ctrl_row.addWidget(self._auto_catch_check)
        ctrl_row.addStretch()
        gl.addLayout(ctrl_row)

        return grp

    # ---- Sphere detection — two-column layout ----

    def _build_detection_group(self) -> QGroupBox:
        grp = QGroupBox("Sphere Detection")
        outer = QHBoxLayout(grp)
        outer.setSpacing(10)

        # ── Left column: Catching Thresholds ──────────────────────────
        catch_box = QGroupBox("Catching Thresholds")
        catch_box.setStyleSheet("QGroupBox { font-weight: bold; }")
        cl = QVBoxLayout(catch_box)
        cl.setSpacing(5)

        hint_c = QLabel("RMS above threshold during shaking → sphere on dropper")
        hint_c.setStyleSheet("color: #6b7280; font-size: 10px;")
        hint_c.setWordWrap(True)
        cl.addWidget(hint_c)

        for axis, thresh_attr, rms_attr, led_attr, default, dec in [
            ("X", "_x_thresh_spin", "_x_rms_lbl", "_x_led", 0.05,  4),
            ("Y", "_y_thresh_spin", "_y_rms_lbl", "_y_led", 0.05,  4),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{axis} thresh:"))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1e7)
            sp.setValue(default)
            sp.setDecimals(dec)
            sp.setFixedWidth(88)
            setattr(self, thresh_attr, sp)
            row.addWidget(sp)
            row.addWidget(QLabel("RMS:"))
            lbl = QLabel("—")
            lbl.setFixedWidth(68)
            lbl.setStyleSheet("color: #6b7280;")
            setattr(self, rms_attr, lbl)
            row.addWidget(lbl)
            led = _led()
            setattr(self, led_attr, led)
            row.addWidget(led)
            row.addWidget(QLabel(f"{axis}"))
            row.addStretch()
            cl.addLayout(row)

        # Veto row
        cl.addWidget(_hsep())
        vr = QHBoxLayout()
        self._veto_led = _led()
        vr.addWidget(self._veto_led)
        vl = QLabel("Veto active")
        vl.setStyleSheet("color: #d97706; font-weight: bold;")
        vr.addWidget(vl)
        vr.addSpacing(8)
        for label, attr, default in [
            ("Post-veto:", "_post_veto_spin", 1.0),
            ("Window:",    "_win_spin",        1.0),
        ]:
            vr.addWidget(QLabel(label))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 60.0)
            sp.setValue(default)
            sp.setDecimals(1)
            sp.setSuffix(" s")
            sp.setFixedWidth(68)
            setattr(self, attr, sp)
            vr.addWidget(sp)
            vr.addSpacing(4)
        vr.addStretch()
        cl.addLayout(vr)

        outer.addWidget(catch_box)

        # ── Right column: Trapping + Lock Thresholds ──────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        trap_box = QGroupBox("Trapping Thresholds")
        trap_box.setStyleSheet("QGroupBox { font-weight: bold; }")
        tl = QVBoxLayout(trap_box)
        tl.setSpacing(5)

        hint_t = QLabel("RMS above threshold after retraction → sphere in trap")
        hint_t.setStyleSheet("color: #6b7280; font-size: 10px;")
        hint_t.setWordWrap(True)
        tl.addWidget(hint_t)

        for axis, thresh_attr, led_attr, default, dec in [
            ("X", "_trap_x_thresh_spin", "_trap_x_led", 0.05, 4),
            ("Y", "_trap_y_thresh_spin", "_trap_y_led", 0.05, 4),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{axis} thresh:"))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1e7)
            sp.setValue(default)
            sp.setDecimals(dec)
            sp.setFixedWidth(88)
            setattr(self, thresh_attr, sp)
            row.addWidget(sp)
            led = _led()
            setattr(self, led_attr, led)
            row.addWidget(led)
            row.addWidget(QLabel(f"{axis} trapped"))
            row.addStretch()
            tl.addLayout(row)

        # Z — moved here from Catching (dropper-out context, same as X/Y trapping)
        z_trap_row = QHBoxLayout()
        z_trap_row.addWidget(QLabel("Z thresh:"))
        self._z_thresh_spin = QDoubleSpinBox()
        self._z_thresh_spin.setRange(0.0, 1e7)
        self._z_thresh_spin.setValue(100.0)
        self._z_thresh_spin.setDecimals(1)
        self._z_thresh_spin.setFixedWidth(88)
        z_trap_row.addWidget(self._z_thresh_spin)
        z_trap_row.addWidget(QLabel("RMS:"))
        self._z_rms_lbl = QLabel("—")
        self._z_rms_lbl.setFixedWidth(68)
        self._z_rms_lbl.setStyleSheet("color: #6b7280;")
        z_trap_row.addWidget(self._z_rms_lbl)
        self._z_led = _led()
        z_trap_row.addWidget(self._z_led)
        z_trap_row.addWidget(QLabel("Z present"))
        z_trap_row.addStretch()
        tl.addLayout(z_trap_row)

        right_col.addWidget(trap_box)

        lock_box = QGroupBox("Lock Thresholds")
        lock_box.setStyleSheet("QGroupBox { font-weight: bold; }")
        ll = QVBoxLayout(lock_box)
        ll.setSpacing(5)

        hint_l = QLabel("RMS below threshold → sphere stable and locked")
        hint_l.setStyleSheet("color: #6b7280; font-size: 10px;")
        hint_l.setWordWrap(True)
        ll.addWidget(hint_l)

        for axis, thresh_attr, led_attr, default, dec in [
            ("X", "_lock_x_thresh_spin", "_lock_x_led", 0.02,  4),
            ("Y", "_lock_y_thresh_spin", "_lock_y_led", 0.02,  4),
            ("Z", "_lock_z_thresh_spin", "_lock_z_led", 50.0,  1),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{axis} thresh:"))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1e7)
            sp.setValue(default)
            sp.setDecimals(dec)
            sp.setFixedWidth(88)
            setattr(self, thresh_attr, sp)
            row.addWidget(sp)
            led = _led()
            setattr(self, led_attr, led)
            row.addWidget(led)
            row.addWidget(QLabel(f"{axis} locked"))
            row.addStretch()
            ll.addLayout(row)

        right_col.addWidget(lock_box)
        right_col.addStretch()

        right_w = QWidget()
        right_w.setLayout(right_col)
        outer.addWidget(right_w)

        return grp

    # ---- Feedback presets ----

    def _build_presets_group(self) -> QGroupBox:
        grp = QGroupBox("Feedback Presets")
        pg = QGridLayout()
        pg.setSpacing(5)

        for col, text in [(1, "Prepare Trap"), (2, "Start Feedback"), (3, "Current FPGA")]:
            hdr = QLabel(f"<b>{text}</b>")
            hdr.setAlignment(Qt.AlignCenter)
            pg.addWidget(hdr, 0, col)

        for row, (field, _reg, is_int) in enumerate(PRESET_FIELDS, start=1):
            pg.addWidget(QLabel(field), row, 0)

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

            rb_lbl = QLabel("—")
            rb_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rb_lbl.setStyleSheet("color: #6b7280;")
            rb_lbl.setFixedWidth(120)
            self._preset_readback_lbls[field] = rb_lbl
            pg.addWidget(rb_lbl, row, 3)

        arow = len(PRESET_FIELDS) + 1
        prep_btn = QPushButton("Prepare Trap")
        prep_btn.setMinimumWidth(120)
        prep_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 8px; "
            "background-color: #7c3aed; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #9333ea; }"
        )
        prep_btn.clicked.connect(lambda: self._write_preset("prepare"))
        pg.addWidget(prep_btn, arow, 1)

        fb_btn = QPushButton("Start Feedback")
        fb_btn.setMinimumWidth(120)
        fb_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 8px; "
            "background-color: #2563eb; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        fb_btn.clicked.connect(lambda: self._write_preset("feedback"))
        pg.addWidget(fb_btn, arow, 2)

        stop_fb_btn = QPushButton("Stop Feedback")
        stop_fb_btn.setMinimumWidth(120)
        stop_fb_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 8px; "
            "background-color: #dc2626; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )
        stop_fb_btn.setToolTip("Set all feedback gains (dg X/Y/Z, ig Z, pg Z) to 0")
        stop_fb_btn.clicked.connect(self._stop_feedback)
        pg.addWidget(stop_fb_btn, arow + 1, 2)

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
        bi.addSpacing(12)

        bi.addWidget(QLabel("Raise:"))
        self._raise_amount_spin = QSpinBox()
        self._raise_amount_spin.setRange(1, 32767)
        self._raise_amount_spin.setValue(500)
        self._raise_amount_spin.setSuffix(" cts")
        self._raise_amount_spin.setFixedWidth(90)
        self._raise_amount_spin.setToolTip("Amount to add to DC offset Z when Raise Trap is clicked")
        bi.addWidget(self._raise_amount_spin)

        raise_btn = QPushButton("Raise Trap")
        raise_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 12px; "
            "background-color: #0891b2; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #06b6d4; }"
        )
        raise_btn.clicked.connect(self._on_raise_trap)
        bi.addWidget(raise_btn)
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

    # ---- Trapping macros ----

    def _build_macro_group(self) -> QGroupBox:
        grp = QGroupBox("Trapping Macros")
        gl = QVBoxLayout(grp)
        gl.setSpacing(8)

        # Status LED row
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status:"))

        self._macro_caught_led = _led()
        status_row.addWidget(self._macro_caught_led)
        status_row.addWidget(QLabel("Caught"))
        status_row.addSpacing(10)

        self._macro_trapped_led = _led()
        status_row.addWidget(self._macro_trapped_led)
        status_row.addWidget(QLabel("Trapped"))
        status_row.addSpacing(10)

        self._macro_locked_led = _led()
        status_row.addWidget(self._macro_locked_led)
        status_row.addWidget(QLabel("Locked"))
        status_row.addSpacing(20)

        self._macro_status_lbl = QLabel("Idle")
        self._macro_status_lbl.setStyleSheet("color: #6b7280; font-style: italic;")
        status_row.addWidget(self._macro_status_lbl, stretch=1)
        gl.addLayout(status_row)

        # Token display row — live view of hardware + sphere state
        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("Tokens:"))
        self._token_lbls: dict[str, QLabel] = {}
        for key in ("dropper", "laser", "feedback", "shaker", "sphere"):
            token_row.addSpacing(4)
            hdr = QLabel(f"{key}:")
            hdr.setStyleSheet("color: #374151; font-size: 10px;")
            token_row.addWidget(hdr)
            lbl = QLabel("—")
            lbl.setStyleSheet("color: #6b7280; font-size: 10px; font-weight: bold;")
            lbl.setFixedWidth(72)
            self._token_lbls[key] = lbl
            token_row.addWidget(lbl)
        token_row.addStretch()
        gl.addLayout(token_row)

        gl.addWidget(_hsep())

        # Timing / threshold settings row
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Trap check timeout:"))
        self._trap_timeout_spin = QDoubleSpinBox()
        self._trap_timeout_spin.setRange(5.0, 300.0)
        self._trap_timeout_spin.setValue(30.0)
        self._trap_timeout_spin.setDecimals(0)
        self._trap_timeout_spin.setSuffix(" s")
        self._trap_timeout_spin.setFixedWidth(80)
        self._trap_timeout_spin.setToolTip(
            "Give up if trap condition not met within this time")
        settings_row.addWidget(self._trap_timeout_spin)
        settings_row.addSpacing(16)

        settings_row.addWidget(QLabel("Confirm ticks:"))
        self._fail_limit_spin = QSpinBox()
        self._fail_limit_spin.setRange(1, 30)
        self._fail_limit_spin.setValue(5)
        self._fail_limit_spin.setFixedWidth(55)
        self._fail_limit_spin.setToolTip(
            "Consecutive 250 ms ticks required to confirm lock (or to declare lock lost)")
        settings_row.addWidget(self._fail_limit_spin)
        settings_row.addStretch()
        gl.addLayout(settings_row)

        # Automation parameters row
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("DC offset high:"))
        self._dc_offset_high_spin = QSpinBox()
        self._dc_offset_high_spin.setRange(-32768, 32767)
        self._dc_offset_high_spin.setValue(32000)
        self._dc_offset_high_spin.setFixedWidth(80)
        self._dc_offset_high_spin.setToolTip(
            "DC offset Z written during PREPARING to raise the sphere before catching")
        auto_row.addWidget(self._dc_offset_high_spin)
        auto_row.addSpacing(16)

        auto_row.addWidget(QLabel("Dropper move timeout:"))
        self._dropper_timeout_spin = QDoubleSpinBox()
        self._dropper_timeout_spin.setRange(1.0, 120.0)
        self._dropper_timeout_spin.setValue(10.0)
        self._dropper_timeout_spin.setDecimals(0)
        self._dropper_timeout_spin.setSuffix(" s")
        self._dropper_timeout_spin.setFixedWidth(75)
        self._dropper_timeout_spin.setToolTip(
            "Max seconds to wait for dropper to arrive at target position")
        auto_row.addWidget(self._dropper_timeout_spin)
        auto_row.addSpacing(16)

        auto_row.addWidget(QLabel("FB retries:"))
        self._fb_retry_spin = QSpinBox()
        self._fb_retry_spin.setRange(0, 10)
        self._fb_retry_spin.setValue(2)
        self._fb_retry_spin.setFixedWidth(50)
        self._fb_retry_spin.setToolTip(
            "Stop + restart feedback this many times before giving up and re-lowering")
        auto_row.addWidget(self._fb_retry_spin)
        auto_row.addSpacing(16)

        auto_row.addWidget(QLabel("Run until:"))
        self._endpoint_combo = QComboBox()
        self._endpoint_combo.addItems(_ENDPOINT_NAMES)
        self._endpoint_combo.setCurrentText(
            _SETTINGS.value("macro/endpoint", "Lock"))
        self._endpoint_combo.setFixedWidth(120)
        self._endpoint_combo.setToolTip(
            "Stop the automation sequence at this milestone")
        self._endpoint_combo.currentTextChanged.connect(
            lambda t: _SETTINGS.setValue("macro/endpoint", t))
        auto_row.addWidget(self._endpoint_combo)
        auto_row.addStretch()
        gl.addLayout(auto_row)

        # Button row
        btn_row = QHBoxLayout()

        self._macro_trap_btn = QPushButton("Trap Sphere")
        self._macro_trap_btn.setFixedHeight(36)
        self._macro_trap_btn.setMinimumWidth(140)
        self._macro_trap_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._macro_trap_btn.clicked.connect(self._on_macro_trap)
        btn_row.addWidget(self._macro_trap_btn)

        self._macro_continue_btn = QPushButton("Continue Trapping")
        self._macro_continue_btn.setFixedHeight(36)
        self._macro_continue_btn.setMinimumWidth(160)
        self._macro_continue_btn.setStyleSheet(
            "QPushButton { background-color: #7c3aed; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #9333ea; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._macro_continue_btn.clicked.connect(self._on_macro_continue)
        btn_row.addWidget(self._macro_continue_btn)

        self._macro_release_btn = QPushButton("Release Sphere")
        self._macro_release_btn.setFixedHeight(36)
        self._macro_release_btn.setMinimumWidth(140)
        self._macro_release_btn.setStyleSheet(
            "QPushButton { background-color: #d97706; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #f59e0b; }"
        )
        self._macro_release_btn.clicked.connect(self._on_macro_release)
        btn_row.addWidget(self._macro_release_btn)

        self._macro_force_btn = QPushButton("Force →")
        self._macro_force_btn.setFixedHeight(36)
        self._macro_force_btn.setMinimumWidth(110)
        self._macro_force_btn.setStyleSheet(
            "QPushButton { background-color: #0891b2; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #06b6d4; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )
        self._macro_force_btn.setToolTip(
            "Manually advance the macro one step, bypassing threshold checks")
        self._macro_force_btn.clicked.connect(self._on_macro_force_continue)
        btn_row.addWidget(self._macro_force_btn)

        self._macro_abort_btn = QPushButton("■  Abort")
        self._macro_abort_btn.setFixedHeight(36)
        self._macro_abort_btn.setMinimumWidth(100)
        self._macro_abort_btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )
        self._macro_abort_btn.clicked.connect(self._on_macro_abort)
        btn_row.addWidget(self._macro_abort_btn)

        btn_row.addStretch()
        gl.addLayout(btn_row)

        return grp

    # ------------------------------------------------------------------
    # Fast data handling (main thread via queued signal)
    # ------------------------------------------------------------------

    def _on_fast_data_ui(self, values: dict) -> None:
        t       = time.monotonic()
        in_veto = self._shaking or t < self._veto_until
        win_s   = self._win_spin.value()
        cutoff  = t - win_s

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

        # Cache for lower timer and macro state machine
        self._x_rms  = x_rms
        self._y_rms  = y_rms
        self._z_mean = z_mean
        self._z_rms  = z_rms

        # Update display
        self._x_rms_lbl.setText(f"{x_rms:.4f}")
        self._y_rms_lbl.setText(f"{y_rms:.4f}")
        self._z_rms_lbl.setText(f"{z_rms:.1f}")
        self._avg_z_lbl.setText(f"{z_mean:.1f}")
        self._rms_z_disp_lbl.setText(f"{z_rms:.1f}")
        self._z_setpoint_hint_lbl.setText(f"(avg: {z_mean:.0f})")

        # Catching detection thresholds (above = sphere detected)
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

        # Trapping thresholds (above = sphere present after retraction)
        x_trap = x_rms > self._trap_x_thresh_spin.value()
        y_trap = y_rms > self._trap_y_thresh_spin.value()

        if x_trap != self._x_trapped:
            self._x_trapped = x_trap
            self._trap_x_led.setStyleSheet(_LED_ON if x_trap else _LED_OFF)
        if y_trap != self._y_trapped:
            self._y_trapped = y_trap
            self._trap_y_led.setStyleSheet(_LED_ON if y_trap else _LED_OFF)

        # Lock thresholds (below = sphere stable / locked)
        x_lock = x_rms < self._lock_x_thresh_spin.value()
        y_lock = y_rms < self._lock_y_thresh_spin.value()
        z_lock = z_rms < self._lock_z_thresh_spin.value()

        if x_lock != self._x_locked:
            self._x_locked = x_lock
            self._lock_x_led.setStyleSheet(_LED_ON if x_lock else _LED_OFF)
        if y_lock != self._y_locked:
            self._y_locked = y_lock
            self._lock_y_led.setStyleSheet(_LED_ON if y_lock else _LED_OFF)
        if z_lock != self._z_locked:
            self._z_locked = z_lock
            self._lock_z_led.setStyleSheet(_LED_ON if z_lock else _LED_OFF)

        # Auto-catch: stop shaking when both X and Y detected outside veto
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
            self._x_buf.clear()
            self._y_buf.clear()
        elif event_type == "sweep_start":
            self._shaking = True
        elif event_type == "step":
            self._shaking = False
            self._veto_until = time.monotonic() + self._post_veto_spin.value()
        elif event_type == "done":
            self._shaking = False
            self._veto_until = time.monotonic() + self._post_veto_spin.value()
            self._macro_sweep_done = True   # consumed by _on_macro_tick SHAKING handler

    # ------------------------------------------------------------------
    # Dropper shortcuts
    # ------------------------------------------------------------------

    def _move_preset(self, name: str) -> None:
        if self._dropper_widget is None:
            return
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

    def _on_resume_shaking(self) -> None:
        if self._shaker_widget is None:
            return
        ok = self._shaker_widget.resume_shaking_public()
        if not ok:
            return
        # If the macro is idle, re-enter the SHAKING watch state so catch
        # detection is active while shaking resumes.
        if self._macro_state == _ST_IDLE:
            self._macro_caught_led.setStyleSheet(_LED_OFF)
            self._macro_trapped_led.setStyleSheet(_LED_OFF)
            self._macro_locked_led.setStyleSheet(_LED_OFF)
            self._tokens["shaker"] = "running"
            self._enter_state(_ST_SHAKING)
            self._macro_status_lbl.setText("Resumed shaking — waiting for sphere catch…")
            if not self._macro_timer.isActive():
                self._macro_timer.start(250)

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

    def _on_raise_trap(self) -> None:
        if self._fpga is None or not self._fpga.is_connected:
            return
        try:
            current = float(self._fpga.read_register("DC offset Z"))
        except Exception:
            return
        new_val = min(current + self._raise_amount_spin.value(), 32767.0)
        try:
            self._fpga.write_register("DC offset Z", round(new_val))
        except Exception:
            pass

    def _on_lower_timer(self) -> None:
        if not self._lowering:
            return

        mean_z     = self._z_mean
        z_setpoint = self._z_setpoint_spin.value()
        tolerance  = self._z_tol_spin.value()

        # Freeze: Z detected AND mean near setpoint
        if self._z_detected and abs(mean_z - z_setpoint) <= tolerance:
            self._lowering = False
            self._lower_timer.stop()
            try:
                if self._fpga is not None and self._fpga.is_connected:
                    self._fpga.write_register("DC offset Z", round(self._current_dc))
                    # Sync Z Setpoint register to the value used for the freeze
                    self._fpga.write_register("Z Setpoint", z_setpoint)
            except Exception:
                pass
            # Push the captured Z setpoint into both preset columns so the user
            # doesn't have to manually update them before pressing Prepare Trap / Start Feedback
            for key in ("prepare", "feedback"):
                sp = self._preset_spins[key].get("Z Setpoint")
                if sp is not None:
                    sp.setValue(float(z_setpoint))
            self._center_led.setStyleSheet(_LED_ON)
            self._center_lbl.setVisible(True)
            return

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
                continue
            val = self._preset_spins[key][field].value()
            try:
                self._fpga.write_register(reg, val)
            except Exception:
                pass

    def _stop_feedback(self) -> None:
        if self._fpga is None or not self._fpga.is_connected:
            return
        for field, reg, is_int in PRESET_FIELDS:
            if is_int:
                continue  # skip Z Setpoint and DC offset Z
            try:
                self._fpga.write_register(reg, 0.0)
            except Exception:
                pass

    def update_preset_readbacks(self, state: dict) -> None:
        """Update gray readback labels from an FPGA register snapshot (slow poll, ~5 Hz)."""
        for field, reg, is_int in PRESET_FIELDS:
            val = state.get(reg)
            if val is None:
                continue
            text = f"{int(round(val))}" if is_int else f"{val:.4f}"
            lbl = self._preset_readback_lbls.get(field)
            if lbl is not None:
                lbl.setText(text)
            if field == "DC offset Z" and self._dc_offset_fb_lbl is not None:
                self._dc_offset_fb_lbl.setText(text)
            if field == "DC offset Z" and not self._lowering:
                self._lower_dc_offset_lbl.setText(text)

    # ------------------------------------------------------------------
    # Macro helpers
    # ------------------------------------------------------------------

    def _refresh_dropper_pos(self) -> None:
        if self._dropper_widget is not None:
            self._dropper_pos_lbl.setText(
                f"{self._dropper_widget._position_mm:.3f} mm")

    def _enter_state(self, state: int) -> None:
        self._macro_state         = state
        self._macro_state_entry_t = time.monotonic()
        self._macro_tick_count    = 0
        self._macro_stable_reads  = 0
        self._update_token_display()

    def _update_token_display(self) -> None:
        _tok_color = {
            "unknown": "#6b7280", "moving": "#d97706", "dropping": "#16a34a",
            "retracted": "#9333ea", "high": "#dc2626", "low": "#2563eb",
            "off": "#6b7280", "on": "#16a34a", "stopped": "#6b7280",
            "running": "#d97706", "exhausted": "#dc2626",
            "none": "#6b7280", "caught": "#d97706", "trapped": "#2563eb",
            "focused": "#7c3aed", "locked": "#16a34a",
        }
        for key, lbl in self._token_lbls.items():
            val = self._tokens.get(key, "—")
            lbl.setText(val)
            lbl.setStyleSheet(
                f"color: {_tok_color.get(val, '#6b7280')}; "
                f"font-size: 10px; font-weight: bold;")

    def _dropper_at(self, preset: str) -> bool:
        """True when dropper has been within 0.1 mm of preset for 5 consecutive reads."""
        if self._dropper_widget is None:
            return False
        target = self._dropper_spins[preset].value()
        pos    = self._dropper_widget._position_mm
        if abs(pos - target) <= 0.1:
            self._macro_stable_reads += 1
        else:
            self._macro_stable_reads = 0
        return self._macro_stable_reads >= 5

    def _advance_or_stop(self, next_state: int, msg: str) -> None:
        """Advance to next_state, or stop at IDLE if we just completed the selected endpoint."""
        endpoint_state = _ENDPOINT_STATES.get(
            self._endpoint_combo.currentText(), _ST_LOCKING)
        if self._macro_state == endpoint_state:
            self._enter_state(_ST_IDLE)
            self._macro_timer.stop()
            self._macro_status_lbl.setText(
                f"Done ({self._endpoint_combo.currentText()}). {msg}")
        else:
            self._enter_state(next_state)
            self._macro_status_lbl.setText(msg)

    def _restart_sequence(self) -> None:
        """Abort all active sub-processes and re-enter PREPARING."""
        if self._lowering:
            self._on_stop_lower()
        if self._shaker_widget and self._shaker_widget.is_shaking():
            self._shaker_widget.request_stop()
        self._stop_feedback()
        self._tokens = {
            "dropper": "unknown", "laser": "unknown", "feedback": "off",
            "shaker": "stopped",  "sphere": "none",
        }
        self._macro_caught_led.setStyleSheet(_LED_OFF)
        self._macro_trapped_led.setStyleSheet(_LED_OFF)
        self._macro_locked_led.setStyleSheet(_LED_OFF)
        self._macro_fb_retries    = 0
        self._macro_fb_restarting = False
        self._macro_sweep_done    = False
        self._enter_state(_ST_PREPARING)
        # PREPARING enter actions
        self._move_preset("dropping")
        self._tokens["dropper"] = "moving"
        self._tokens["laser"]   = "high"
        if self._fpga is not None and self._fpga.is_connected:
            try:
                self._fpga.write_register(
                    "DC offset Z", round(self._dc_offset_high_spin.value()))
            except Exception:
                pass
        self._macro_status_lbl.setText("Preparing — moving dropper to dropping position…")
        self._update_token_display()

    # ------------------------------------------------------------------
    # Macro button handlers
    # ------------------------------------------------------------------

    def _on_macro_trap(self) -> None:
        """Start full sequence from PREPARING."""
        if self._macro_state != _ST_IDLE:
            return
        if self._shaker_widget is None:
            self._macro_status_lbl.setText("No shaker connected")
            return
        self._restart_sequence()
        if not self._macro_timer.isActive():
            self._macro_timer.start(250)

    def _on_macro_continue(self) -> None:
        """Skip to CHECKING — use when dropper is already retracted and sphere may be in trap."""
        if self._macro_state != _ST_IDLE:
            return
        self._tokens["sphere"]  = "caught"
        self._tokens["dropper"] = "retracted"
        self._macro_caught_led.setStyleSheet(_LED_ON)
        self._macro_trapped_led.setStyleSheet(_LED_OFF)
        self._macro_locked_led.setStyleSheet(_LED_OFF)
        self._enter_state(_ST_CHECKING)
        self._macro_status_lbl.setText("Skipped to trap check — watching X/Y thresholds…")
        if not self._macro_timer.isActive():
            self._macro_timer.start(250)

    def _on_macro_release(self) -> None:
        """Release sphere: DC offset Z → 0."""
        if self._fpga is None or not self._fpga.is_connected:
            self._macro_status_lbl.setText("FPGA not connected")
            return
        try:
            self._fpga.write_register("DC offset Z", 0)
        except Exception:
            pass
        self._tokens["sphere"] = "none"
        self._update_token_display()
        self._macro_status_lbl.setText("Released — DC offset Z set to 0")

    def _on_macro_abort(self) -> None:
        self._macro_timer.stop()
        if self._shaker_widget and self._shaker_widget.is_shaking():
            self._shaker_widget.request_stop()
        if self._lowering:
            self._on_stop_lower()
        self._tokens = {
            "dropper": "unknown", "laser": "unknown", "feedback": "off",
            "shaker": "stopped",  "sphere": "none",
        }
        self._enter_state(_ST_IDLE)
        self._macro_status_lbl.setText("Aborted")

    def _on_macro_force_continue(self) -> None:
        """Advance the macro one step, bypassing all threshold checks."""
        self._macro_tick_count    = 0
        self._macro_stable_reads  = 0
        self._macro_fb_restarting = False

        if self._macro_state == _ST_PREPARING:
            self._tokens["dropper"] = "dropping"
            self._advance_or_stop(_ST_SHAKING, "Force: starting shaker…")
            if self._macro_state == _ST_SHAKING:
                self._shaker_widget.start_shaking_public(
                    start_v=self._sk_start_v.value(), step_v=self._sk_step_v.value(),
                    n_steps=self._sk_steps.value(),  dwell_s=self._sk_dwell.value(),
                    max_v=self._sk_max_v.value())
                self._tokens["shaker"] = "running"

        elif self._macro_state == _ST_SHAKING:
            if self._shaker_widget:
                self._shaker_widget.request_stop()
            self._tokens["sphere"] = "caught"
            self._tokens["shaker"] = "stopped"
            self._macro_caught_led.setStyleSheet(_LED_ON)
            self._advance_or_stop(_ST_RETRACTING, "Force: caught — retracting dropper…")
            if self._macro_state == _ST_RETRACTING:
                self._move_preset("retraction")
                self._tokens["dropper"] = "moving"

        elif self._macro_state == _ST_RETRACTING:
            self._tokens["dropper"] = "retracted"
            self._advance_or_stop(_ST_CHECKING, "Force: checking trap…")

        elif self._macro_state == _ST_CHECKING:
            self._tokens["sphere"] = "trapped"
            self._macro_trapped_led.setStyleSheet(_LED_ON)
            self._advance_or_stop(_ST_LOWERING, "Force: trapped — lowering sphere…")
            if self._macro_state == _ST_LOWERING:
                self._on_lower_sphere()

        elif self._macro_state == _ST_LOWERING:
            if self._lowering:
                self._on_stop_lower()
            self._tokens["sphere"] = "focused"
            self._advance_or_stop(_ST_FB_START, "Force: at focus — starting feedback…")
            if self._macro_state == _ST_FB_START:
                self._write_preset("feedback")
                self._tokens["feedback"] = "on"

        elif self._macro_state == _ST_FB_START:
            self._advance_or_stop(_ST_LOCKING, "Force: watching for lock…")

        elif self._macro_state == _ST_LOCKING:
            self._tokens["sphere"] = "locked"
            self._macro_locked_led.setStyleSheet(_LED_ON)
            self._advance_or_stop(_ST_LOCKED, "Force: marked locked.")

        if not self._macro_timer.isActive() and self._macro_state != _ST_IDLE:
            self._macro_timer.start(250)
        self._update_token_display()

    # ------------------------------------------------------------------
    # Macro state machine tick (250 ms)
    # ------------------------------------------------------------------

    def _on_macro_tick(self) -> None:
        now     = time.monotonic()
        elapsed = now - self._macro_state_entry_t

        if self._macro_state == _ST_IDLE:
            self._macro_timer.stop()
            return

        # ── PREPARING: wait for dropper to reach dropping position ──────
        elif self._macro_state == _ST_PREPARING:
            timeout = self._dropper_timeout_spin.value()
            if self._dropper_at("dropping") or elapsed >= timeout:
                self._tokens["dropper"] = "dropping"
                self._advance_or_stop(
                    _ST_SHAKING, "Dropper at dropping position — starting shaker…")
                if self._macro_state == _ST_SHAKING:
                    ok = self._shaker_widget.start_shaking_public(
                        start_v=self._sk_start_v.value(),
                        step_v=self._sk_step_v.value(),
                        n_steps=self._sk_steps.value(),
                        dwell_s=self._sk_dwell.value(),
                        max_v=self._sk_max_v.value(),
                    )
                    if not ok:
                        self._macro_status_lbl.setText(
                            "Cannot start shaker — AWG/PSU not connected")
                        self._enter_state(_ST_IDLE)
                        self._macro_timer.stop()
                    else:
                        self._tokens["shaker"] = "running"
            else:
                self._macro_status_lbl.setText(
                    f"Moving dropper to dropping position… ({elapsed:.1f}/{timeout:.0f}s)")

        # ── SHAKING: watch catch condition, check for exhaustion ────────
        elif self._macro_state == _ST_SHAKING:
            if self._macro_sweep_done:
                self._macro_sweep_done = False
                last_v = getattr(self._shaker_widget, "_last_voltage", 0.0)
                if last_v >= 60.0:
                    self._tokens["shaker"] = "exhausted"
                    self._enter_state(_ST_EXHAUSTED)
                    self._macro_timer.stop()
                    self._macro_status_lbl.setText(
                        "Dropper exhausted (reached 60 V) — load new slide")
                    self._update_token_display()
                    return

            in_veto = self._shaking or now < self._veto_until
            if not in_veto and self._catch_cond.check(self):
                if self._shaker_widget:
                    self._shaker_widget.request_stop()
                self._tokens["sphere"] = "caught"
                self._tokens["shaker"] = "stopped"
                self._macro_caught_led.setStyleSheet(_LED_ON)
                self._advance_or_stop(
                    _ST_RETRACTING, "Caught! Moving dropper to retraction…")
                if self._macro_state == _ST_RETRACTING:
                    self._move_preset("retraction")
                    self._tokens["dropper"] = "moving"
            elif elapsed >= self._trap_timeout_spin.value():
                self._macro_status_lbl.setText("Shaking timeout — restarting from PREPARING…")
                self._restart_sequence()

        # ── RETRACTING: wait for dropper to reach retraction position ───
        elif self._macro_state == _ST_RETRACTING:
            timeout = self._dropper_timeout_spin.value()
            if self._dropper_at("retraction") or elapsed >= timeout:
                self._tokens["dropper"] = "retracted"
                self._advance_or_stop(
                    _ST_CHECKING, "Dropper retracted — checking trap thresholds…")
            else:
                self._macro_status_lbl.setText(
                    f"Retracting dropper… ({elapsed:.1f}/{timeout:.0f}s)")

        # ── CHECKING: X/Y RMS above trap threshold = sphere in trap ─────
        elif self._macro_state == _ST_CHECKING:
            if self._trap_cond.check(self):
                self._tokens["sphere"] = "trapped"
                self._macro_trapped_led.setStyleSheet(_LED_ON)
                self._advance_or_stop(
                    _ST_LOWERING, "Sphere in trap! Lowering to focus…")
                if self._macro_state == _ST_LOWERING:
                    self._on_lower_sphere()
            elif elapsed >= self._trap_timeout_spin.value():
                self._macro_status_lbl.setText(
                    "Trap check timeout — sphere lost, restarting…")
                self._restart_sequence()
            else:
                self._macro_status_lbl.setText(
                    f"Checking trap…  X:{self._x_rms:.4f}  Y:{self._y_rms:.4f}  "
                    f"({elapsed:.0f}/{self._trap_timeout_spin.value():.0f}s)")

        # ── LOWERING: DC offset Z ramp (Lower Sphere), watch for focus ──
        elif self._macro_state == _ST_LOWERING:
            if not self._lowering:
                # Lower sphere finished — check if sphere is at focus
                if self._focus_cond.check(self):
                    self._tokens["sphere"] = "focused"
                    self._tokens["laser"]  = "low"
                    self._advance_or_stop(
                        _ST_FB_START, "Sphere at focus — writing feedback preset…")
                    if self._macro_state == _ST_FB_START:
                        self._write_preset("feedback")
                        self._tokens["feedback"] = "on"
                        self._macro_fb_retries    = 0
                        self._macro_fb_restarting = False
                else:
                    # Lowering stopped but Z not detected — sphere likely lost
                    self._macro_status_lbl.setText(
                        "Z not detected after lowering — sphere lost, restarting…")
                    self._restart_sequence()

        # ── FB_START: one settling tick then move to LOCKING ────────────
        elif self._macro_state == _ST_FB_START:
            self._advance_or_stop(_ST_LOCKING, "Feedback running — watching for lock…")

        # ── LOCKING: N consecutive ticks below lock thresholds ──────────
        elif self._macro_state == _ST_LOCKING:
            n = self._fail_limit_spin.value()

            # One-tick feedback restart cycle
            if self._macro_fb_restarting:
                self._macro_fb_restarting = False
                self._write_preset("feedback")
                self._tokens["feedback"] = "on"
                self._macro_tick_count = 0
                self._macro_status_lbl.setText(
                    f"Feedback restarted ({self._macro_fb_retries}/"
                    f"{self._fb_retry_spin.value()}) — watching for lock…")
                return

            if self._lock_cond.check(self):
                self._macro_tick_count += 1
                self._macro_status_lbl.setText(
                    f"Confirming lock ({self._macro_tick_count}/{n})  "
                    f"X:{self._x_rms:.4f}  Y:{self._y_rms:.4f}  Z:{self._z_rms:.1f}")
                if self._macro_tick_count >= n:
                    self._tokens["sphere"] = "locked"
                    self._macro_locked_led.setStyleSheet(_LED_ON)
                    self._advance_or_stop(_ST_LOCKED, "Locked! Sphere stable.")
            else:
                self._macro_tick_count = 0
                if self._macro_fb_retries < self._fb_retry_spin.value():
                    self._stop_feedback()
                    self._tokens["feedback"] = "off"
                    self._macro_fb_retries   += 1
                    self._macro_fb_restarting = True
                    self._macro_status_lbl.setText(
                        f"Lock failed — stopping feedback, retry "
                        f"{self._macro_fb_retries}/{self._fb_retry_spin.value()}…")
                else:
                    self._macro_status_lbl.setText(
                        "All feedback retries exhausted — re-lowering sphere…")
                    self._stop_feedback()
                    self._tokens["feedback"] = "off"
                    self._enter_state(_ST_LOWERING)
                    self._on_lower_sphere()

        # ── LOCKED: keep monitoring; N bad ticks → retry LOCKING ────────
        elif self._macro_state == _ST_LOCKED:
            n = self._fail_limit_spin.value()
            if self._lock_cond.check(self):
                self._macro_tick_count = 0
                self._macro_status_lbl.setText(
                    f"Locked — sphere stable.  "
                    f"X:{self._x_rms:.4f}  Y:{self._y_rms:.4f}  Z:{self._z_rms:.1f}")
            else:
                self._macro_tick_count += 1
                self._macro_status_lbl.setText(
                    f"Locked — instability warning ({self._macro_tick_count}/{n})  "
                    f"X:{self._x_rms:.4f}  Y:{self._y_rms:.4f}  Z:{self._z_rms:.1f}")
                if self._macro_tick_count >= n:
                    self._tokens["sphere"] = "focused"
                    self._macro_locked_led.setStyleSheet(_LED_OFF)
                    self._macro_fb_retries    = 0
                    self._macro_fb_restarting = False
                    self._enter_state(_ST_LOCKING)
                    self._macro_status_lbl.setText(
                        "Lock lost — retrying feedback sequence…")

        self._update_token_display()

    # ------------------------------------------------------------------
    # Session state
    # ------------------------------------------------------------------

    def get_ui_state(self) -> dict:
        return {
            "prepare":  {f: sp.value() for f, sp in self._preset_spins["prepare"].items()},
            "feedback": {f: sp.value() for f, sp in self._preset_spins["feedback"].items()},
            "lower_sphere": {
                "dac_per_s":    self._dac_per_s_spin.value(),
                "lower_limit":  self._lower_limit_spin.value(),
                "z_setpoint":   self._z_setpoint_spin.value(),
                "tolerance":    self._z_tol_spin.value(),
                "interval_ms":  self._interval_spin.value(),
                "raise_amount": self._raise_amount_spin.value(),
            },
            "detection": {
                "x_thresh":      self._x_thresh_spin.value(),
                "y_thresh":      self._y_thresh_spin.value(),
                "z_thresh":      self._z_thresh_spin.value(),
                "post_veto":     self._post_veto_spin.value(),
                "window_s":      self._win_spin.value(),
                "trap_x_thresh": self._trap_x_thresh_spin.value(),
                "trap_y_thresh": self._trap_y_thresh_spin.value(),
                "lock_x_thresh": self._lock_x_thresh_spin.value(),
                "lock_y_thresh": self._lock_y_thresh_spin.value(),
                "lock_z_thresh": self._lock_z_thresh_spin.value(),
            },
            "dropper_shortcuts": {n: sp.value() for n, sp in self._dropper_spins.items()},
            "shaker_shortcuts": {
                "start_v": self._sk_start_v.value(),
                "step_v":  self._sk_step_v.value(),
                "n_steps": self._sk_steps.value(),
                "dwell_s": self._sk_dwell.value(),
                "max_v":   self._sk_max_v.value(),
            },
            "macro": {
                "trap_timeout":    self._trap_timeout_spin.value(),
                "fail_limit":      self._fail_limit_spin.value(),
                "dc_offset_high":  self._dc_offset_high_spin.value(),
                "dropper_timeout": self._dropper_timeout_spin.value(),
                "fb_retries":      self._fb_retry_spin.value(),
            },
        }

    def restore_ui_state(self, state: dict) -> None:
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
        _set("_dac_per_s_spin",    ls, "dac_per_s")
        _set("_lower_limit_spin",  ls, "lower_limit")
        _set("_z_setpoint_spin",   ls, "z_setpoint")
        _set("_z_tol_spin",        ls, "tolerance")
        _set("_interval_spin",     ls, "interval_ms")
        _set("_raise_amount_spin", ls, "raise_amount")

        det = state.get("detection", {})
        _set("_x_thresh_spin",       det, "x_thresh")
        _set("_y_thresh_spin",       det, "y_thresh")
        _set("_z_thresh_spin",       det, "z_thresh")
        _set("_post_veto_spin",      det, "post_veto")
        _set("_win_spin",            det, "window_s")
        _set("_trap_x_thresh_spin",  det, "trap_x_thresh")
        _set("_trap_y_thresh_spin",  det, "trap_y_thresh")
        _set("_lock_x_thresh_spin",  det, "lock_x_thresh")
        _set("_lock_y_thresh_spin",  det, "lock_y_thresh")
        _set("_lock_z_thresh_spin",  det, "lock_z_thresh")

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

        mac = state.get("macro", {})
        _set("_trap_timeout_spin",   mac, "trap_timeout")
        _set("_fail_limit_spin",     mac, "fail_limit")
        _set("_dc_offset_high_spin", mac, "dc_offset_high")
        _set("_dropper_timeout_spin",mac, "dropper_timeout")
        _set("_fb_retry_spin",       mac, "fb_retries")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        self._lower_timer.stop()
        self._macro_timer.stop()
        self._pos_timer.stop()


# ---------------------------------------------------------------------------
# Procedure class
# ---------------------------------------------------------------------------

class Procedure(ControlProcedure):
    NAME            = "Trapping"
    PERSISTENT      = True
    WANTS_FAST_DATA = True
    REQUIRES        = ["Dropper Stage", "Shake Dropper"]
    DESCRIPTION = (
        "Sphere trapping workflow: dropper stage shortcuts, shake-dropper "
        "shortcuts with auto-catch, X/Y/Z RMS catching / trapping / lock thresholds, "
        "feedback presets with Z setpoint, lower-sphere DC ramp with auto-freeze, "
        "and Trap Sphere / Continue / Release macros."
    )

    def __init__(self):
        self._widget: TrappingPanel | None = None
        self._dropper_widget = None
        self._shaker_widget  = None

    def set_instruments(self, dropper_widget, shaker_widget) -> None:
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

    def on_fast_data(self, values: dict) -> None:
        if self._widget is not None:
            self._widget.on_fast_data(values)

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
