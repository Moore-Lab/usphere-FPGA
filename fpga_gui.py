"""
fpga_gui.py

PyQt5 GUI for NI PXIe-7856R FPGA control.
Mirrors the LabVIEW front panel layout with tabbed sections for PID
feedback (X/Y/Z), arbitrary waveform, EOM/COM output, AO rotation,
and real-time monitoring.

Run with:
    python fpga_gui.py
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fpga.core import FPGAConfig, FPGAController, _append_log
from session_state import load_state as _load_session_state, save_state as _save_session_state
from modules import discover_hardware_modules
from procedures.base import LiveFPGAFacade
from fpga.registers import (
    Access,
    Category,
    HOST_PARAM_DEFAULTS,
    HOST_PARAM_MAP,
    REGISTER_MAP,
    REGISTERS,
    RegisterDef,
    host_params_by_category,
    names_by_category,
    writable_registers,
)
from fpga.plot import ALL_PLOT_NAMES, FPGAPlotWidget
from fpga.ipc import TICPublisher
from arb_waveform import (
    WaveformResult,
    generate_comb,
    generate_sine,
    generate_triangle,
    generate_trapezoid,
)


# ---------------------------------------------------------------------------
# Thread-safe signal bridge
# ---------------------------------------------------------------------------

class _Signals(QObject):
    status_message = pyqtSignal(str)
    registers_updated = pyqtSignal(dict)
    plot_data = pyqtSignal(dict)
    connected = pyqtSignal()
    disconnected = pyqtSignal()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_edit(value: float = 0.0, readonly: bool = False,
               width: int = 100) -> QLineEdit:
    """Create a QLineEdit for a numeric value."""
    edit = QLineEdit(_fmt(value))
    edit.setAlignment(Qt.AlignRight)
    edit.setFixedWidth(width)
    if readonly:
        edit.setReadOnly(True)
        edit.setStyleSheet("background: #f0f0f0;")
    return edit


def _fmt(v: float, is_integer: bool = False) -> str:
    if is_integer:
        return str(int(round(v)))
    if v != 0 and (abs(v) < 0.001 or abs(v) >= 1e6):
        return f"{v:.6e}"
    # Strip trailing zeros but keep at least 3 decimal places
    s = f"{v:.6f}".rstrip("0")
    dot_pos = s.find(".")
    if dot_pos == -1:
        return s + ".000"
    decimals = len(s) - dot_pos - 1
    if decimals < 3:
        s += "0" * (3 - decimals)
    return s


def _float(edit: QLineEdit) -> float:
    try:
        return float(edit.text().strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Hardware modules tab
# ---------------------------------------------------------------------------

class _ModulesWidget(QWidget):
    """
    Config and test panel for hardware modules (modules/mod_*.py).

    Discovers modules at construction time.  Each module gets a collapsible
    QGroupBox whose fields are driven by the module's CONFIG_FIELDS list.
    A "Test" button runs module.test() in a worker thread and reports the
    result inline.

    Call get_all_configs() to retrieve {MODULE_NAME: {key: value}} for
    passing to procedures that need to talk to instruments.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modules = discover_hardware_modules()
        self._fields: dict[str, dict[str, QLineEdit]] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._test_workers: list[QThread] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        if not self._modules:
            layout.addWidget(QLabel("No hardware modules found in modules/"))
        else:
            for mod in self._modules:
                layout.addWidget(self._make_module_group(mod))

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _make_module_group(self, mod) -> QGroupBox:
        grp = QGroupBox(mod.DEVICE_NAME)
        grp.setCheckable(True)
        gl = QVBoxLayout(grp)

        field_widgets: dict[str, QLineEdit] = {}
        for field in mod.CONFIG_FIELDS:
            key      = field["key"]
            label    = field.get("label", key)
            default  = field.get("default", "")
            tooltip  = field.get("tooltip", "")

            row = QHBoxLayout()
            lbl = QLabel(f"{label}:")
            lbl.setFixedWidth(180)
            row.addWidget(lbl)
            edit = QLineEdit(str(default))
            edit.setFixedWidth(150)
            if tooltip:
                edit.setToolTip(tooltip)
            row.addWidget(edit)
            row.addStretch()
            gl.addLayout(row)
            field_widgets[key] = edit

        self._fields[mod.MODULE_NAME] = field_widgets

        # Test button + inline status
        test_row = QHBoxLayout()
        test_btn = QPushButton("Test")
        test_btn.setFixedWidth(70)
        test_btn.clicked.connect(lambda _, m=mod: self._run_test(m))
        status_lbl = QLabel("")
        status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        test_row.addWidget(test_btn)
        test_row.addWidget(status_lbl, stretch=1)
        gl.addLayout(test_row)

        self._status_labels[mod.MODULE_NAME] = status_lbl
        return grp

    def _run_test(self, mod) -> None:
        config = self.get_module_config(mod.MODULE_NAME)
        lbl = self._status_labels[mod.MODULE_NAME]
        lbl.setText("Testing…")
        lbl.setStyleSheet("color: gray; font-size: 10px;")

        class _Worker(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, m, cfg):
                super().__init__()
                self._m, self._cfg = m, cfg
            def run(self):
                ok, msg = self._m.test(self._cfg)
                self.done.emit(ok, msg)

        w = _Worker(mod, config)
        w.done.connect(lambda ok, msg, n=mod.MODULE_NAME: self._on_test_done(n, ok, msg))
        w.finished.connect(lambda: self._test_workers.remove(w) if w in self._test_workers else None)
        self._test_workers.append(w)
        w.start()

    def _on_test_done(self, module_name: str, ok: bool, msg: str) -> None:
        lbl = self._status_labels.get(module_name)
        if lbl:
            lbl.setText(msg)
            lbl.setStyleSheet(
                f"color: {'green' if ok else 'red'}; font-size: 10px;")

    def get_module_config(self, module_name: str) -> dict:
        """Return {key: value} for a single module's config fields."""
        return {k: e.text().strip()
                for k, e in self._fields.get(module_name, {}).items()}

    def get_all_configs(self) -> dict[str, dict]:
        """Return {MODULE_NAME: {key: value}} for all modules."""
        return {name: self.get_module_config(name) for name in self._fields}


# ---------------------------------------------------------------------------
# Resources panel — unified plugin lifecycle manager
# ---------------------------------------------------------------------------

class _ResourcesWidget(QWidget):
    """
    Unified Resources panel.  Discovers all ControlProcedure plugins
    (proc_*.py) and lets the user connect / disconnect each one.

    PERSISTENT procedures are auto-loaded on startup via auto_load_persistent().
    Procedures that declare REQUIRES = [name, ...] only become connectable once
    their named dependencies are already connected.

    Public surface used by FPGAWidget
    ----------------------------------
    auto_load_persistent()         — call once in _build_ui after creation
    notify_fpga_update(state)      — forward slow-poll snapshot to loaded procs
    notify_fast_data(values)       — forward fast-plot data (WANTS_FAST_DATA=True)
    get_ui_states()                — {loaded:[...], state:{name:dict}} for persistence
    restore_ui_states(data)        — reverse; accepts both new and legacy formats
    teardown_all()                 — teardown all loaded procs on app close
    """

    _LED_OFF = ("background-color: #d1d5db; border-radius: 7px; "
                "border: 1px solid #9ca3af;")
    _LED_ON  = ("background-color: #22c55e; border-radius: 7px; "
                "border: 1px solid #16a34a;")

    def __init__(self, tabs: QTabWidget, fpga_controller: FPGAController,
                 parent=None):
        super().__init__(parent)
        self._tabs   = tabs
        self._ctrl   = fpga_controller
        self._facade = LiveFPGAFacade(self._ctrl)

        self._loaded:    dict[str, tuple] = {}          # name → (proc, widget)
        self._buttons:   dict[str, QPushButton] = {}
        self._leds:      dict[str, QLabel] = {}
        self._dep_lbls:  dict[str, QLabel] = {}
        self._available: list = []

        try:
            from procedures import discover_all_procedures
            self._available = discover_all_procedures()
        except Exception as exc:
            print(f"[resources] Discovery failed: {exc}")

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
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

        header = QLabel(
            "<b>Resources</b><br>"
            "<span style='color:#6b7280; font-size:10px;'>"
            "Connect a resource to open its control tab.  "
            "Resources with dependencies must have those connected first.</span>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        if not self._available:
            layout.addWidget(QLabel("No proc_*.py files found in procedures/"))
        else:
            for cls in self._available:
                layout.addWidget(self._make_row(cls))

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _make_row(self, cls) -> QGroupBox:
        grp = QGroupBox()
        gl = QVBoxLayout(grp)
        gl.setContentsMargins(8, 6, 8, 6)
        gl.setSpacing(3)

        hr = QHBoxLayout()
        led = QLabel()
        led.setFixedSize(14, 14)
        led.setStyleSheet(self._LED_OFF)
        self._leds[cls.NAME] = led
        hr.addWidget(led)

        hr.addWidget(QLabel(f"<b>{cls.NAME}</b>"), stretch=1)

        btn = QPushButton("Connect")
        btn.setFixedWidth(100)
        btn.setFixedHeight(28)
        self._style_connect(btn)
        btn.clicked.connect(lambda _, c=cls: self._toggle(c))
        self._buttons[cls.NAME] = btn
        hr.addWidget(btn)
        gl.addLayout(hr)

        desc = getattr(cls, "DESCRIPTION", "")
        if desc:
            dl = QLabel(desc)
            dl.setWordWrap(True)
            dl.setStyleSheet("color: #6b7280; font-size: 10px; margin-left: 20px;")
            gl.addWidget(dl)

        requires = getattr(cls, "REQUIRES", [])
        if requires:
            dep_lbl = QLabel(f"Requires: {', '.join(requires)}")
            dep_lbl.setStyleSheet(
                "color: #9ca3af; font-size: 10px; margin-left: 20px;")
            self._dep_lbls[cls.NAME] = dep_lbl
            gl.addWidget(dep_lbl)

        return grp

    @staticmethod
    def _style_connect(btn: QPushButton) -> None:
        btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; font-size: 11px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )

    @staticmethod
    def _style_disconnect(btn: QPushButton) -> None:
        btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-weight: bold; font-size: 11px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    def _toggle(self, cls) -> None:
        if cls.NAME in self._loaded:
            self._disconnect(cls.NAME)
        else:
            self._connect(cls)

    def _connect(self, cls) -> None:
        requires = getattr(cls, "REQUIRES", [])
        if any(r not in self._loaded for r in requires):
            return

        proc = cls()
        proc.fpga = self._facade

        # Wire dependencies before create_widget so the widget has them on init
        if requires and hasattr(proc, "set_instruments"):
            dep_widgets = [self._loaded[dep][1] for dep in requires]
            proc.set_instruments(*dep_widgets)

        try:
            widget = proc.create_widget()
        except Exception as exc:
            print(f"[resources] {cls.NAME} create_widget failed: {exc}")
            return

        self._tabs.addTab(widget, cls.NAME)
        self._loaded[cls.NAME] = (proc, widget)
        self._tabs.setCurrentWidget(widget)

        self._leds[cls.NAME].setStyleSheet(self._LED_ON)
        btn = self._buttons[cls.NAME]
        btn.setText("Disconnect")
        self._style_disconnect(btn)

        self._refresh_buttons()

    def _disconnect(self, name: str) -> None:
        # Block if a loaded proc depends on this one
        for cls in self._available:
            if cls.NAME != name and cls.NAME in self._loaded:
                if name in getattr(cls, "REQUIRES", []):
                    return

        proc, widget = self._loaded.pop(name)
        idx = self._tabs.indexOf(widget)
        if idx >= 0:
            self._tabs.removeTab(idx)
            widget.deleteLater()
        try:
            proc.teardown()
        except Exception as exc:
            print(f"[resources] {name} teardown error: {exc}")

        self._leds[name].setStyleSheet(self._LED_OFF)
        btn = self._buttons[name]
        btn.setText("Connect")
        self._style_connect(btn)

        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        """Enable/disable Connect buttons; update dependency status labels."""
        for cls in self._available:
            btn = self._buttons.get(cls.NAME)
            if btn is None:
                continue
            if cls.NAME in self._loaded:
                # Can disconnect unless something loaded depends on it
                blocked = any(
                    cls.NAME in getattr(c, "REQUIRES", [])
                    for c in self._available
                    if c.NAME in self._loaded and c.NAME != cls.NAME
                )
                btn.setEnabled(not blocked)
                continue
            requires = getattr(cls, "REQUIRES", [])
            btn.setEnabled(all(r in self._loaded for r in requires))
            dep_lbl = self._dep_lbls.get(cls.NAME)
            if dep_lbl and requires:
                parts = []
                for r in requires:
                    if r in self._loaded:
                        parts.append(
                            f"<span style='color:#16a34a;'>{r} ✓</span>")
                    else:
                        parts.append(
                            f"<span style='color:#dc2626;'>{r} ✗</span>")
                dep_lbl.setText(f"Requires: {', '.join(parts)}")

    # ------------------------------------------------------------------
    # Auto-load
    # ------------------------------------------------------------------

    def auto_load_persistent(self, saved_names: list | None = None) -> None:
        """Load PERSISTENT procedures on startup (or a specific saved list)."""
        targets = saved_names or [
            cls.NAME for cls in self._available
            if getattr(cls, "PERSISTENT", False)
        ]
        for cls in self._available:
            if cls.NAME in targets:
                self._connect(cls)

    # ------------------------------------------------------------------
    # Data forwarding
    # ------------------------------------------------------------------

    def notify_fpga_update(self, state: dict) -> None:
        for name, (proc, _) in self._loaded.items():
            try:
                proc.on_fpga_update(state)
            except Exception as exc:
                print(f"[resources] {name} on_fpga_update error: {exc}")

    def notify_fast_data(self, values: dict) -> None:
        for name, (proc, _) in self._loaded.items():
            if getattr(type(proc), "WANTS_FAST_DATA", False):
                try:
                    proc.on_fast_data(values)
                except Exception as exc:
                    print(f"[resources] {name} on_fast_data error: {exc}")

    # ------------------------------------------------------------------
    # Session state
    # ------------------------------------------------------------------

    def get_ui_states(self) -> dict:
        return {
            "loaded": list(self._loaded.keys()),
            "state":  {n: p.get_ui_state() for n, (p, _) in self._loaded.items()},
        }

    def restore_ui_states(self, data: dict) -> None:
        """Accepts both new format {loaded:[...], state:{name:dict}}
        and the legacy flat format {name: dict}."""
        states = data.get("state", data)   # legacy: data IS the states dict
        for name, (proc, _) in self._loaded.items():
            s = states.get(name)
            if s:
                try:
                    proc.restore_ui_state(s)
                except Exception as exc:
                    print(f"[resources] {name} restore_ui_state error: {exc}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown_all(self) -> None:
        for name in list(self._loaded.keys()):
            proc, widget = self._loaded.pop(name)
            try:
                proc.teardown()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Waveform designer — Monte Carlo comb worker
# ---------------------------------------------------------------------------

class _CombMCWorker(QThread):
    """Background thread that runs generate_comb() Monte Carlo optimisation."""
    progress = pyqtSignal(int, int, float)   # trial_idx, total, best_rms
    finished = pyqtSignal(object)            # WaveformResult

    def __init__(self, n_points: int, sample_rate: float,
                 frequencies: list, n_trials: int,
                 stop_flag: list):
        super().__init__()
        self._n_points     = n_points
        self._sample_rate  = sample_rate
        self._frequencies  = frequencies
        self._n_trials     = n_trials
        self._stop_flag    = stop_flag

    def run(self) -> None:
        result = generate_comb(
            self._n_points,
            self._sample_rate,
            self._frequencies,
            n_trials=self._n_trials,
            progress_cb=lambda i, t, r: self.progress.emit(i, t, r),
            stop_flag=self._stop_flag,
        )
        self.finished.emit(result)


# ---------------------------------------------------------------------------
# Edwards TIC — background poll worker
# ---------------------------------------------------------------------------

# Ensure EDWARDS-TIC is importable
_TIC_DIR = Path(__file__).parent / "resources" / "EDWARDS-TIC"
if _TIC_DIR.exists() and str(_TIC_DIR) not in sys.path:
    sys.path.insert(0, str(_TIC_DIR))

# Ensure solenoid and butterfly valve submodules are importable
_SOLENOID_DIR = Path(__file__).parent / "resources" / "Solenoid-valve-controller"
if _SOLENOID_DIR.exists() and str(_SOLENOID_DIR) not in sys.path:
    sys.path.insert(0, str(_SOLENOID_DIR))

_CV_DIR = Path(__file__).parent / "resources" / "IdealVac-CommandValve"
if _CV_DIR.exists() and str(_CV_DIR) not in sys.path:
    sys.path.insert(0, str(_CV_DIR))


class _TICPollWorker(QThread):
    """Polls the TIC (gauges + pump telemetry) off the GUI thread."""
    finished = pyqtSignal(dict)   # get_status() result
    error    = pyqtSignal(str)

    def __init__(self, ctrl):
        super().__init__()
        self._ctrl = ctrl

    def run(self) -> None:
        try:
            self.finished.emit(self._ctrl.get_status())
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FPGAWidget(QWidget):
    """
    Embeddable FPGA control panel.

    Pass *controller* to reuse an existing FPGAController (e.g. from
    ctrl_server.py running in the same process).  When omitted a new
    controller is created for standalone use.
    """

    def __init__(self, controller: FPGAController | None = None):
        super().__init__()

        # Signal bridge for thread safety
        self._signals = _Signals()
        self._signals.status_message.connect(self._append_status)
        self._signals.registers_updated.connect(self._on_registers_updated)
        self._signals.plot_data.connect(self._on_plot_data)
        self._signals.connected.connect(self._on_connected)
        self._signals.disconnected.connect(self._on_disconnected)

        # Backend controller — shared with server when embedded
        if controller is not None:
            self._ctrl = controller
            self._ctrl._on_status             = self._signals.status_message.emit
            self._ctrl._on_registers_updated  = self._signals.registers_updated.emit
            self._ctrl._on_plot_data          = self._signals.plot_data.emit
            self._ctrl._on_connected          = self._signals.connected.emit
            self._ctrl._on_disconnected       = self._signals.disconnected.emit
        else:
            self._ctrl = FPGAController(
                on_status=self._signals.status_message.emit,
                on_registers_updated=self._signals.registers_updated.emit,
                on_plot_data=self._signals.plot_data.emit,
                on_connected=self._signals.connected.emit,
                on_disconnected=self._signals.disconnected.emit,
            )

        # Widget maps (populated during build)
        self._reg_edits: dict[str, QLineEdit] = {}       # FPGA register widgets
        self._reg_live_labels: dict[str, QLabel] = {}  # live-value display (writable regs only)
        self._host_edits: dict[str, QLineEdit] = {}     # host-param widgets
        self._host_values: dict[str, float] = dict(HOST_PARAM_DEFAULTS)
        self._bead_fb_combos: dict[str, QComboBox] = {}  # per-axis bead fb selector
        self._boost_multiplier: float = 10.0              # boost gain factor

        # Edwards TIC state
        self._tic_publisher   = TICPublisher()
        self._tic_ctrl        = None   # TICController instance (kept alive while connected)
        self._tic_poll_worker: _TICPollWorker | None = None
        self._tic_cmd_workers: list = []  # keep command QThreads alive until finished
        self._tic_timer       = QTimer(self)
        self._tic_timer.timeout.connect(self._on_tic_poll)

        # Valve state
        self._last_pump_state: dict = {}          # updated by TIC poll — used for safety checks
        self._sv_ctrl         = None              # ValveController (solenoid)
        self._bv_ctrl         = None              # CommandValveController (butterfly)
        self._bv_position: float | None = None   # last known butterfly position (degrees)
        self._bv_ramp_stop: threading.Event | None = None  # set to cancel an in-progress ramp
        self._valve_cmd_workers: list = []        # keep valve QThreads alive

        # Waveform designer state
        self._wd_result = None          # last generated WaveformResult
        self._wd_mc_worker: _CombMCWorker | None = None
        self._wd_mc_stop_flag: list[bool] = [False]
        self._wd_has_plot: bool = False  # set in _build_waveform_designer

        # Session state
        self._session_state: dict = {}         # loaded by _restore_full_state
        self._last_register_values: dict = {}  # updated by _on_registers_updated

        self._build_ui()
        self._restore_full_state()

        # Autosave every 5 s + on close
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave_state)
        self._autosave_timer.start(5000)

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        _layout = QVBoxLayout(self)
        _layout.setContentsMargins(0, 0, 0, 0)
        _layout.addWidget(tabs)

        tabs.addTab(self._build_connection_tab(), "Connection")
        tabs.addTab(self._build_feedback_tab(), "Feedback")
        tabs.addTab(self._build_waveform_tab(), "Waveform")
        tabs.addTab(self._build_outputs_tab(), "Outputs")
        tabs.addTab(self._build_registers_tab(), "All Registers")
        tabs.addTab(self._build_plot_tab(), "Monitor")
        tabs.addTab(self._build_tic_tab(), "Vacuum")

        # Hardware modules config / test panel
        self._modules_widget = _ModulesWidget()
        tabs.addTab(self._modules_widget, "Modules")

        # Unified resource manager — discovers + loads all proc_*.py plugins
        self._resources = _ResourcesWidget(tabs, self._ctrl)
        tabs.addTab(self._resources, "Resources")
        # Auto-load PERSISTENT procedures (tabs appear after Resources tab)
        self._resources.auto_load_persistent()

    # ------------------------------------------------------------------
    # Connection tab
    # ------------------------------------------------------------------

    def _build_connection_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left = QVBoxLayout()

        # Connection group
        conn_grp = QGroupBox("Connection")
        cl = QVBoxLayout()

        bf_row = QHBoxLayout()
        bf_row.addWidget(QLabel("Bitfile:"))
        self._bitfile_edit = QLineEdit(self._ctrl.config.bitfile)
        bf_row.addWidget(self._bitfile_edit, 1)
        bf_browse = QPushButton("Browse")
        bf_browse.clicked.connect(self._browse_bitfile)
        bf_row.addWidget(bf_browse)
        cl.addLayout(bf_row)

        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("Resource:"))
        self._resource_edit = QLineEdit(self._ctrl.config.resource)
        self._resource_edit.setFixedWidth(120)
        res_row.addWidget(self._resource_edit)
        res_row.addStretch()
        cl.addLayout(res_row)

        poll_row = QHBoxLayout()
        poll_row.addWidget(QLabel("Poll interval:"))
        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(50, 5000)
        self._poll_spin.setValue(self._ctrl.config.poll_interval_ms)
        self._poll_spin.setSuffix(" ms")
        poll_row.addWidget(self._poll_spin)
        poll_row.addStretch()
        cl.addLayout(poll_row)

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        btn_row.addWidget(self._connect_btn)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        btn_row.addWidget(self._disconnect_btn)
        cl.addLayout(btn_row)

        self._conn_label = QLabel("Disconnected")
        self._conn_label.setStyleSheet("color: red; font-weight: bold;")
        cl.addWidget(self._conn_label)
        conn_grp.setLayout(cl)
        left.addWidget(conn_grp)

        # Actions
        act_grp = QGroupBox("Actions")
        al = QVBoxLayout()

        read_btn = QPushButton("Read All Registers")
        read_btn.clicked.connect(self._on_read_all)
        al.addWidget(read_btn)

        self._start_mon_btn = QPushButton("Start Monitor")
        self._start_mon_btn.clicked.connect(self._on_start_monitor)
        al.addWidget(self._start_mon_btn)

        self._stop_mon_btn = QPushButton("Stop Monitor")
        self._stop_mon_btn.clicked.connect(self._on_stop_monitor)
        al.addWidget(self._stop_mon_btn)

        snap_row = QHBoxLayout()
        save_snap = QPushButton("Save Snapshot")
        save_snap.clicked.connect(self._on_save_snapshot)
        snap_row.addWidget(save_snap)
        load_snap = QPushButton("Load Snapshot")
        load_snap.clicked.connect(self._on_load_snapshot)
        snap_row.addWidget(load_snap)
        al.addLayout(snap_row)

        sphere_row = QHBoxLayout()
        save_sphere = QPushButton("Save Sphere")
        save_sphere.clicked.connect(self._on_save_sphere)
        sphere_row.addWidget(save_sphere)
        load_sphere = QPushButton("Load Sphere")
        load_sphere.clicked.connect(self._on_load_sphere)
        sphere_row.addWidget(load_sphere)
        al.addLayout(sphere_row)

        act_grp.setLayout(al)
        left.addWidget(act_grp)

        # Global indicators
        glob_grp = QGroupBox("Global")
        gl = QGridLayout()
        row = 0
        for name in ["Big Number", "Count(uSec)", "FPGA Error Out", "Stop",
                      "X_emergency_threshould", "Y_emergency_threshould",
                      "No_integral_gain", "master x", "master y"]:
            reg = REGISTER_MAP.get(name)
            if reg is None:
                continue
            gl.addWidget(QLabel(name), row, 0)
            ro = (reg.access == Access.READ)
            w = 50 if reg.is_bool else 100
            edit = _make_edit(0.0, readonly=ro, width=w)
            self._reg_edits[name] = edit
            gl.addWidget(edit, row, 1)
            if not ro:
                btn = QPushButton("Set")
                btn.setFixedWidth(40)
                btn.clicked.connect(lambda _, n=name: self._write_one(n))
                gl.addWidget(btn, row, 2)
            row += 1
        glob_grp.setLayout(gl)
        left.addWidget(glob_grp)
        left.addStretch()

        layout.addLayout(left, 2)

        # Status log
        status_grp = QGroupBox("Status")
        sl = QVBoxLayout()
        self._status_text = QTextEdit()
        self._status_text.setReadOnly(True)
        self._status_text.setFont(QFont("Consolas", 9))
        sl.addWidget(self._status_text)
        status_grp.setLayout(sl)
        layout.addWidget(status_grp, 3)

        return widget

    # ------------------------------------------------------------------
    # Feedback tab — X, Y, Z side by side
    # ------------------------------------------------------------------

    def _build_feedback_tab(self) -> QWidget:
        """Single tab with X, Y, Z feedback controls in three side-by-side columns."""
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        for axis in ("X", "Y", "Z"):
            splitter.addWidget(self._build_axis_column(axis))

        outer.addWidget(splitter)
        return widget

    def _build_axis_column(self, axis: str) -> QWidget:
        """Scrollable column of PID controls for a single axis."""
        a  = axis.upper()
        al = axis.lower()

        # Coloured header strip
        _header_styles = {
            "X": ("background:#ede9fe; color:#5b21b6;"),
            "Y": ("background:#dcfce7; color:#166534;"),
            "Z": ("background:#dbeafe; color:#1e40af;"),
        }
        col = QWidget()
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(0)

        header = QLabel(f"  {a} Axis")
        header.setStyleSheet(
            _header_styles[a] +
            " font-size: 13px; font-weight: bold; padding: 5px 8px;"
        )
        col_layout.addWidget(header)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)

        # ---------- Bead Feedback (after chamber) ----------
        bead_grp = QGroupBox("Bead Feedback")
        bg = QGridLayout()
        r = 0
        bg.addWidget(QLabel("Bead feedback:"), r, 0)
        fb_combo = QComboBox()
        fb_combo.addItems(["Normal", "Inverted"])
        self._bead_fb_combos[a] = fb_combo
        bg.addWidget(fb_combo, r, 1)
        r += 1
        r = self._add_reg(bg, r, f"{a} Setpoint")
        r = self._add_reg(bg, r, f"DC offset {a}")
        r = self._add_reg(bg, r, f"pg {a}")
        ig_name = f" ig {a}"
        r = self._add_reg(bg, r, ig_name)
        r = self._add_reg(bg, r, f"dg {a}")
        r = self._add_reg(bg, r, f"dg band {a}")
        r = self._add_reg(bg, r, f"pg {a} mod" if a == "Z" else f"dg{al} mod")
        r = self._add_reg(bg, r, f"Upper lim {a}")
        r = self._add_reg(bg, r, f"Lower lim {a}")
        for ind in [f"AI {a} plot", f"fb {a} plot", f"tot_fb {a} plot"]:
            r = self._add_reg(bg, r, ind)
        bead_grp.setLayout(bg)
        layout.addWidget(bead_grp)

        # ---------- Before-chamber PID ----------
        before_grp = QGroupBox(
            "AOM Feedback — Before Chamber" if a == "Z"
            else "Before-Chamber PID")
        bg2 = QGridLayout()
        r = 0
        r = self._add_reg(bg2, r, f"Use {a} PID before")
        r = self._add_reg(bg2, r, f"{a} before Setpoint")
        r = self._add_reg(bg2, r, f"pg {a} before")
        r = self._add_reg(bg2, r, f" ig {a} before")
        r = self._add_reg(bg2, r, f"dg {a} before")
        r = self._add_reg(bg2, r, f"dg band {a} before")
        r = self._add_reg(bg2, r, f"Upper lim {a} before")
        r = self._add_reg(bg2, r, f"Lower lim {a} before")
        for ind in [f"AI {a} before chamber plot",
                    f"fb {a} before chamber plot"]:
            r = self._add_reg(bg2, r, ind)
        before_grp.setLayout(bg2)
        layout.addWidget(before_grp)

        # ---------- Miscellaneous ----------
        misc_grp = QGroupBox("Misc")
        mg = QGridLayout()
        r = 0
        r = self._add_reg(mg, r, f"activate COM{al}")
        r = self._add_reg(mg, r, f"Reset {al} accum")
        if a == "Z":
            r = self._add_reg(mg, r, "accum reset z1")
            r = self._add_reg(mg, r, "accum out z1")
            r = self._add_reg(mg, r, "accurrm reset z2")
            r = self._add_reg(mg, r, "accum out z2")
            r = self._add_reg(mg, r, "pz?")
        misc_grp.setLayout(mg)
        layout.addWidget(misc_grp)

        # ---------- Filter Parameters ----------
        filt_grp = QGroupBox("Filter Parameters (Hz)")
        fg = QGridLayout()
        r = 0
        r = self._add_host(fg, r, f"hp freq {a}")
        r = self._add_host(fg, r, f"lp freq {a}")
        r = self._add_host(fg, r, f"LP FF {a}")
        r = self._add_host(fg, r, f"hp freq {a} before")
        r = self._add_host(fg, r, f"lp freq {a} before")
        if a == "Z":
            r = self._add_host(fg, r, "LP FF Z before")
        r = self._add_host(fg, r, f"hp freq band{a}")
        r = self._add_host(fg, r, f"lp freq band{a}")
        r = self._add_host(fg, r, f"hp freq {a} band before")
        r = self._add_host(fg, r, f"lp freq {a} band before")
        filt_grp.setLayout(fg)
        layout.addWidget(filt_grp)

        # ---------- Notch Filters ----------
        notch_grp = QGroupBox("Notch Filters")
        ng = QGridLayout()
        r = 0
        for i in range(1, 5):
            r = self._add_host(ng, r, f"notch freq {i} {al}")
            r = self._add_host(ng, r, f"notch Q {i} {al}")
        notch_grp.setLayout(ng)
        layout.addWidget(notch_grp)

        # ---------- Computed Coefficients ----------
        coeff_grp = QGroupBox("Computed Coefficients")
        coeff_grp.setCheckable(True)
        coeff_grp.setChecked(False)   # collapsed by default — rarely edited
        cg = QGridLayout()
        r = 0
        coeff_regs = [
            f"HP Coeff {a}", f"LP Coeff {a}",
            f"HP Coeff {a} before", f"LP Coeff {a} before",
            f"HP Coeff band {a}", f"LP Coeff band {a}",
            f"HP coeff band {a} before", f"LP Coeff band {a} before",
            f"final filter coeff {a}",
        ]
        if a == "Z":
            coeff_regs.append("final filter coeff Z before")
        for n in [f"Notch coeff {al} {i}" for i in range(1, 5)]:
            coeff_regs.append(n)
        for name in coeff_regs:
            r = self._add_reg(cg, r, name)
        coeff_grp.setLayout(cg)
        layout.addWidget(coeff_grp)

        # ---------- Z-specific: Ramp Power ----------
        if a == "Z":
            ramp_grp = QGroupBox("Ramp Power (Z Offset)")
            rg = QGridLayout()
            r = 0
            r = self._add_host(rg, r, "End value power")
            r = self._add_host(rg, r, "Step power")
            r = self._add_host(rg, r, "Delay Time (s) power")
            ramp_btn = QPushButton("Ramp Power")
            ramp_btn.clicked.connect(self._on_ramp_power)
            rg.addWidget(ramp_btn, r, 0, 1, 3)
            ramp_grp.setLayout(rg)
            layout.addWidget(ramp_grp)

        # ---------- Action buttons ----------
        btn_layout = QHBoxLayout()
        change_btn = QPushButton("Change Pars 2" if a == "Z" else "Change Pars")
        change_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        change_btn.clicked.connect(lambda _, ax=a: self._on_change_pars(ax))
        btn_layout.addWidget(change_btn)

        reset_btn = QPushButton(f"Reset {al}")
        reset_btn.clicked.connect(
            lambda _, n=f"Reset {al} accum": self._write_one_value(n, 1.0))
        btn_layout.addWidget(reset_btn)

        boost_btn = QPushButton("Boost")
        boost_btn.setStyleSheet(
            "background-color: #ff9800; font-weight: bold; padding: 6px;")
        boost_btn.clicked.connect(lambda _, ax=a: self._on_boost(ax))
        btn_layout.addWidget(boost_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        layout.addStretch()

        scroll.setWidget(container)
        col_layout.addWidget(scroll, stretch=1)
        return col

    # ------------------------------------------------------------------
    # Waveform tab
    # ------------------------------------------------------------------

    def _build_waveform_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        gen_scroll = QScrollArea()
        gen_scroll.setWidgetResizable(True)
        gen_container = QWidget()
        gen_layout = QVBoxLayout(gen_container)
        gen_layout.setContentsMargins(8, 8, 8, 8)
        gen_layout.setSpacing(4)
        gen_layout.addWidget(self._build_waveform_designer())
        gen_layout.addStretch()
        gen_scroll.setWidget(gen_container)
        splitter.addWidget(gen_scroll)

        player_scroll = QScrollArea()
        player_scroll.setWidgetResizable(True)
        player_container = QWidget()
        player_layout = QVBoxLayout(player_container)
        player_layout.setContentsMargins(8, 8, 8, 8)
        player_layout.setSpacing(4)
        player_layout.addWidget(self._build_waveform_player())
        player_scroll.setWidget(player_container)
        splitter.addWidget(player_scroll)

        splitter.setSizes([450, 500])
        outer.addWidget(splitter)
        return widget

    # ------------------------------------------------------------------
    # Waveform designer group
    # ------------------------------------------------------------------

    def _build_waveform_designer(self) -> QGroupBox:
        """Waveform generator: design and save integer DAC-count waveforms."""
        grp = QGroupBox("Waveform Generator")
        layout = QVBoxLayout(grp)
        layout.setSpacing(5)

        # Row 1: Points, sample rate, bit depth
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Points:"))
        self._wd_npoints_combo = QComboBox()
        self._wd_npoints_combo.addItems([str(2**n) for n in range(10, 21)])
        self._wd_npoints_combo.setCurrentText("16384")
        self._wd_npoints_combo.currentTextChanged.connect(self._on_wd_params_changed)
        r1.addWidget(self._wd_npoints_combo)
        r1.addSpacing(8)
        r1.addWidget(QLabel("Sa/s:"))
        self._wd_samplerate_edit = QLineEdit("1000000")
        self._wd_samplerate_edit.setFixedWidth(90)
        self._wd_samplerate_edit.textChanged.connect(self._on_wd_params_changed)
        r1.addWidget(self._wd_samplerate_edit)
        r1.addSpacing(8)
        r1.addWidget(QLabel("Bits:"))
        self._wd_bits_spin = QSpinBox()
        self._wd_bits_spin.setRange(4, 16)
        self._wd_bits_spin.setValue(12)
        self._wd_bits_spin.setFixedWidth(50)
        self._wd_bits_spin.valueChanged.connect(self._on_wd_resolution_changed)
        r1.addWidget(self._wd_bits_spin)
        self._wd_maxcount_lbl = QLabel("max ±2047")
        self._wd_maxcount_lbl.setStyleSheet("color: gray; font-size: 10px;")
        r1.addWidget(self._wd_maxcount_lbl)
        r1.addStretch()
        layout.addLayout(r1)

        # Row 2: Volt cal (annotation only) + Amplitude + DC
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Volt cal:"))
        self._wd_volt_cal_counts = QLineEdit("2047")
        self._wd_volt_cal_counts.setFixedWidth(50)
        r2.addWidget(self._wd_volt_cal_counts)
        r2.addWidget(QLabel("cts ="))
        self._wd_volt_cal_v = QLineEdit("10")
        self._wd_volt_cal_v.setFixedWidth(40)
        r2.addWidget(self._wd_volt_cal_v)
        r2.addWidget(QLabel("V"))
        ann_lbl = QLabel("(annotation only)")
        ann_lbl.setStyleSheet("color: gray; font-size: 10px;")
        r2.addWidget(ann_lbl)
        r2.addSpacing(16)
        r2.addWidget(QLabel("Amplitude (cts):"))
        self._wd_amplitude_spin = QSpinBox()
        self._wd_amplitude_spin.setRange(1, 2047)
        self._wd_amplitude_spin.setValue(2047)
        self._wd_amplitude_spin.setFixedWidth(70)
        r2.addWidget(self._wd_amplitude_spin)
        r2.addSpacing(8)
        r2.addWidget(QLabel("DC (cts):"))
        self._wd_dc_spin = QSpinBox()
        self._wd_dc_spin.setRange(-2047, 2047)
        self._wd_dc_spin.setValue(0)
        self._wd_dc_spin.setFixedWidth(70)
        r2.addWidget(self._wd_dc_spin)
        r2.addStretch()
        layout.addLayout(r2)

        # Row 3: Column selector
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Columns:"))
        self._wd_ncols_spin = QSpinBox()
        self._wd_ncols_spin.setRange(1, 3)
        self._wd_ncols_spin.setValue(1)
        self._wd_ncols_spin.setFixedWidth(45)
        self._wd_ncols_spin.valueChanged.connect(self._on_wd_ncols_changed)
        r3.addWidget(self._wd_ncols_spin)
        r3.addSpacing(8)
        self._wd_col_btns: list[QPushButton] = []
        for i in range(3):
            btn = QPushButton(f"Col {i + 1}")
            btn.setCheckable(True)
            btn.setFixedWidth(55)
            btn.clicked.connect(lambda checked, idx=i: self._on_wd_col_btn_clicked(idx))
            r3.addWidget(btn)
            self._wd_col_btns.append(btn)
        self._wd_col_btns[0].setChecked(True)
        r3.addStretch()
        layout.addLayout(r3)
        self._wd_active_col = 0
        self._wd_columns: list = [None, None, None]
        self._on_wd_ncols_changed(1)

        # Waveform type tabs (single-row compact controls)
        self._wd_type_tabs = QTabWidget()
        self._wd_type_tabs.setTabPosition(QTabWidget.North)

        # Sine
        sine_w = QWidget()
        sg = QHBoxLayout(sine_w)
        sg.setContentsMargins(4, 2, 4, 2)
        sg.addWidget(QLabel("Cycles:"))
        self._wd_sine_ncycles = QSpinBox()
        self._wd_sine_ncycles.setRange(1, 100000)
        self._wd_sine_ncycles.setValue(1)
        self._wd_sine_ncycles.setFixedWidth(75)
        self._wd_sine_ncycles.valueChanged.connect(self._on_wd_params_changed)
        sg.addWidget(self._wd_sine_ncycles)
        self._wd_sine_freq_lbl = QLabel("")
        self._wd_sine_freq_lbl.setStyleSheet("color: gray; font-size: 10px;")
        sg.addWidget(self._wd_sine_freq_lbl)
        sg.addSpacing(16)
        sg.addWidget(QLabel("Phase (°):"))
        self._wd_sine_phase = QLineEdit("0.0")
        self._wd_sine_phase.setFixedWidth(55)
        sg.addWidget(self._wd_sine_phase)
        sg.addStretch()
        self._wd_type_tabs.addTab(sine_w, "Sine")

        # Triangle
        tri_w = QWidget()
        tg = QHBoxLayout(tri_w)
        tg.setContentsMargins(4, 2, 4, 2)
        tg.addWidget(QLabel("Cycles:"))
        self._wd_tri_ncycles = QSpinBox()
        self._wd_tri_ncycles.setRange(1, 100000)
        self._wd_tri_ncycles.setValue(1)
        self._wd_tri_ncycles.setFixedWidth(75)
        self._wd_tri_ncycles.valueChanged.connect(self._on_wd_params_changed)
        tg.addWidget(self._wd_tri_ncycles)
        self._wd_tri_freq_lbl = QLabel("")
        self._wd_tri_freq_lbl.setStyleSheet("color: gray; font-size: 10px;")
        tg.addWidget(self._wd_tri_freq_lbl)
        tg.addSpacing(16)
        tg.addWidget(QLabel("Symmetry:"))
        self._wd_tri_symmetry = QLineEdit("0.5")
        self._wd_tri_symmetry.setFixedWidth(55)
        tg.addWidget(self._wd_tri_symmetry)
        hint = QLabel("0=fall  0.5=tri  1=rise")
        hint.setStyleSheet("color: gray; font-size: 10px;")
        tg.addWidget(hint)
        tg.addStretch()
        self._wd_type_tabs.addTab(tri_w, "Triangle")

        # Trapezoid
        trap_w = QWidget()
        trg = QHBoxLayout(trap_w)
        trg.setContentsMargins(4, 2, 4, 2)
        trg.addWidget(QLabel("Cycles:"))
        self._wd_trap_ncycles = QSpinBox()
        self._wd_trap_ncycles.setRange(1, 100000)
        self._wd_trap_ncycles.setValue(1)
        self._wd_trap_ncycles.setFixedWidth(75)
        self._wd_trap_ncycles.valueChanged.connect(self._on_wd_params_changed)
        trg.addWidget(self._wd_trap_ncycles)
        self._wd_trap_freq_lbl = QLabel("")
        self._wd_trap_freq_lbl.setStyleSheet("color: gray; font-size: 10px;")
        trg.addWidget(self._wd_trap_freq_lbl)
        trg.addSpacing(8)
        trg.addWidget(QLabel("Rise:"))
        self._wd_trap_rise = QLineEdit("0.1")
        self._wd_trap_rise.setFixedWidth(42)
        trg.addWidget(self._wd_trap_rise)
        trg.addWidget(QLabel("High:"))
        self._wd_trap_high = QLineEdit("0.4")
        self._wd_trap_high.setFixedWidth(42)
        trg.addWidget(self._wd_trap_high)
        trg.addWidget(QLabel("Fall:"))
        self._wd_trap_fall = QLineEdit("0.1")
        self._wd_trap_fall.setFixedWidth(42)
        trg.addWidget(self._wd_trap_fall)
        self._wd_trap_low_lbl = QLabel("Low:0.400")
        self._wd_trap_low_lbl.setStyleSheet("color: gray; font-size: 10px;")
        trg.addWidget(self._wd_trap_low_lbl)
        for e in (self._wd_trap_rise, self._wd_trap_high, self._wd_trap_fall):
            e.textChanged.connect(self._on_wd_trap_frac_changed)
        trg.addStretch()
        self._wd_type_tabs.addTab(trap_w, "Trapezoid")

        # Freq Comb
        comb_w = QWidget()
        cl = QVBoxLayout(comb_w)
        cl.setSpacing(3)
        cl.setContentsMargins(4, 2, 4, 2)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._wd_comb_mode = QComboBox()
        self._wd_comb_mode.addItems(
            ["List (Hz)", "Range (arange)", "Trap fractions"])
        mode_row.addWidget(self._wd_comb_mode)
        mode_row.addStretch()
        cl.addLayout(mode_row)

        self._wd_comb_stack = QStackedWidget()

        list_w = QWidget()
        ll = QHBoxLayout(list_w)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Frequencies (Hz):"))
        self._wd_comb_list_edit = QLineEdit(
            "100000, 200000, 300000, 400000, 500000, 600000, 700000")
        ll.addWidget(self._wd_comb_list_edit, 1)
        self._wd_comb_stack.addWidget(list_w)

        arange_w = QWidget()
        ag = QHBoxLayout(arange_w)
        ag.setContentsMargins(0, 0, 0, 0)
        ag.addWidget(QLabel("Start (Hz):"))
        self._wd_comb_arange_start = QLineEdit("100000")
        self._wd_comb_arange_start.setFixedWidth(80)
        ag.addWidget(self._wd_comb_arange_start)
        ag.addWidget(QLabel("Stop:"))
        self._wd_comb_arange_stop = QLineEdit("700000")
        self._wd_comb_arange_stop.setFixedWidth(80)
        ag.addWidget(self._wd_comb_arange_stop)
        ag.addWidget(QLabel("Step:"))
        self._wd_comb_arange_step = QLineEdit("100000")
        self._wd_comb_arange_step.setFixedWidth(80)
        ag.addWidget(self._wd_comb_arange_step)
        ag.addStretch()
        self._wd_comb_stack.addWidget(arange_w)

        frac_w = QWidget()
        fg = QHBoxLayout(frac_w)
        fg.setContentsMargins(0, 0, 0, 0)
        fg.addWidget(QLabel("Trap freq (Hz):"))
        self._wd_comb_trap_freq = QLineEdit("150000")
        self._wd_comb_trap_freq.setFixedWidth(80)
        fg.addWidget(self._wd_comb_trap_freq)
        fg.addWidget(QLabel("Fracs:"))
        self._wd_comb_fracs_edit = QLineEdit("1, 2, 3, 4, 5")
        fg.addWidget(self._wd_comb_fracs_edit)
        fg.addStretch()
        self._wd_comb_stack.addWidget(frac_w)

        self._wd_comb_mode.currentIndexChanged.connect(self._on_wd_comb_mode_changed)
        cl.addWidget(self._wd_comb_stack)

        mc_row = QHBoxLayout()
        mc_row.addWidget(QLabel("MC trials:"))
        self._wd_comb_trials = QLineEdit("1000")
        self._wd_comb_trials.setFixedWidth(60)
        mc_row.addWidget(self._wd_comb_trials)
        mc_row.addStretch()
        cl.addLayout(mc_row)

        self._wd_mc_progress = QProgressBar()
        self._wd_mc_progress.setRange(0, 100)
        self._wd_mc_progress.setFixedHeight(12)
        self._wd_mc_progress.hide()
        cl.addWidget(self._wd_mc_progress)

        self._wd_type_tabs.addTab(comb_w, "Freq Comb")
        self._wd_type_tabs.currentChanged.connect(self._on_wd_params_changed)
        layout.addWidget(self._wd_type_tabs)

        # Generate / Cancel / Save row
        gen_row = QHBoxLayout()
        self._wd_gen_btn = QPushButton("Generate → Col")
        self._wd_gen_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 4px 12px; "
            "background-color: #2563eb; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
            "QPushButton:disabled { background-color: #93c5fd; }")
        self._wd_gen_btn.clicked.connect(self._on_wd_generate)
        gen_row.addWidget(self._wd_gen_btn)
        self._wd_cancel_btn = QPushButton("Cancel")
        self._wd_cancel_btn.setStyleSheet(
            "QPushButton { color: #b91c1c; padding: 4px 8px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #fee2e2; }")
        self._wd_cancel_btn.clicked.connect(self._on_wd_cancel)
        self._wd_cancel_btn.hide()
        gen_row.addWidget(self._wd_cancel_btn)
        gen_row.addSpacing(8)
        save_btn = QPushButton("Write CSV…")
        save_btn.clicked.connect(self._on_wd_save)
        gen_row.addWidget(save_btn)
        self._wd_desc_lbl = QLabel("")
        self._wd_desc_lbl.setStyleSheet(
            "color: #6b7280; font-size: 10px; font-style: italic;")
        gen_row.addWidget(self._wd_desc_lbl, 1)
        layout.addLayout(gen_row)

        # Preview plot with twin x-axis (sample index bottom, time top)
        try:
            import pyqtgraph as pg
            self._wd_plot_widget = pg.PlotWidget(background="w")
            self._wd_plot_widget.setFixedHeight(185)
            pi = self._wd_plot_widget.getPlotItem()
            pi.setLabel("left", "DAC counts")
            pi.setLabel("bottom", "Sample index")
            pi.showGrid(x=True, y=True, alpha=0.25)
            pi.showAxis("top")
            pi.getAxis("top").setLabel("Time")
            self._wd_plot_curve = pi.plot(pen=pg.mkPen("#2563eb", width=1.5))
            self._wd_annot = pg.TextItem(anchor=(0, 1), color=(80, 80, 80))
            self._wd_annot.setZValue(10)
            pi.addItem(self._wd_annot)
            layout.addWidget(self._wd_plot_widget)
            self._wd_has_plot = True
        except ImportError:
            layout.addWidget(QLabel("(install pyqtgraph for waveform preview)"))
            self._wd_has_plot = False

        return grp

    def _build_waveform_player(self) -> QGroupBox:
        """Waveform player: load, display (gain-scaled), write to FPGA."""
        grp = QGroupBox("Waveform Player")
        layout = QVBoxLayout(grp)
        layout.setSpacing(5)

        # Row 1: File path + buttons
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("File:"))
        self._arb_file_edit = QLineEdit()
        r1.addWidget(self._arb_file_edit, 1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_arb_file)
        r1.addWidget(browse_btn)
        load_btn = QPushButton("Load & Preview")
        load_btn.clicked.connect(self._on_player_load)
        r1.addWidget(load_btn)
        self._player_write_btn = QPushButton("Write to FPGA")
        self._player_write_btn.setStyleSheet(
            "QPushButton { padding: 4px 10px; background-color: #059669; "
            "color: white; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #10b981; }"
            "QPushButton:disabled { background-color: #6ee7b7; }")
        self._player_write_btn.clicked.connect(self._on_write_arb_buffers)
        r1.addWidget(self._player_write_btn)
        layout.addLayout(r1)

        # Row 2: Count(uSec) + Arb gains (inline compact)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Count(uSec):"))
        self._player_count_usec_spin = QSpinBox()
        self._player_count_usec_spin.setRange(1, 1000000)
        self._player_count_usec_spin.setValue(10)
        self._player_count_usec_spin.setFixedWidth(75)
        self._player_count_usec_spin.setToolTip(
            "Loop period in µs (10 = 10 µs = 100 kHz). Used for time axis.")
        self._player_count_usec_spin.valueChanged.connect(self._on_player_time_axis_changed)
        r2.addWidget(self._player_count_usec_spin)
        r2.addSpacing(12)
        for ch, attr in [(0, "_player_gain0_spin"), (1, "_player_gain1_spin"),
                         (2, "_player_gain2_spin")]:
            r2.addWidget(QLabel(f"Gain ch{ch}:"))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1000.0)
            sp.setValue(1.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(3)
            sp.setFixedWidth(75)
            setattr(self, attr, sp)
            r2.addWidget(sp)
        self._player_gain0_spin.valueChanged.connect(self._on_player_display_changed)
        set_gain_btn = QPushButton("Set Gains")
        set_gain_btn.setFixedWidth(75)
        set_gain_btn.clicked.connect(self._on_player_set_gains)
        r2.addWidget(set_gain_btn)
        r2.addStretch()
        layout.addLayout(r2)

        # Row 3: Ramp controls
        r3 = QHBoxLayout()
        self._player_ramp_cb = QCheckBox("Ramp Arb")
        self._player_ramp_cb.toggled.connect(self._on_player_ramp_toggled)
        r3.addWidget(self._player_ramp_cb)
        r3.addWidget(QLabel("End gain:"))
        self._player_ramp_end = QDoubleSpinBox()
        self._player_ramp_end.setRange(0.0, 1000.0)
        self._player_ramp_end.setValue(10.0)
        self._player_ramp_end.setDecimals(2)
        self._player_ramp_end.setFixedWidth(65)
        self._player_ramp_end.setEnabled(False)
        self._player_ramp_end.valueChanged.connect(self._on_player_update_ramp_preview)
        r3.addWidget(self._player_ramp_end)
        r3.addWidget(QLabel("Step:"))
        self._player_ramp_step = QDoubleSpinBox()
        self._player_ramp_step.setRange(0.001, 1000.0)
        self._player_ramp_step.setValue(0.5)
        self._player_ramp_step.setDecimals(3)
        self._player_ramp_step.setFixedWidth(65)
        self._player_ramp_step.setEnabled(False)
        self._player_ramp_step.valueChanged.connect(self._on_player_update_ramp_preview)
        r3.addWidget(self._player_ramp_step)
        r3.addWidget(QLabel("Delay (s):"))
        self._player_ramp_delay = QDoubleSpinBox()
        self._player_ramp_delay.setRange(0.001, 3600.0)
        self._player_ramp_delay.setValue(1.0)
        self._player_ramp_delay.setDecimals(3)
        self._player_ramp_delay.setFixedWidth(70)
        self._player_ramp_delay.setEnabled(False)
        r3.addWidget(self._player_ramp_delay)
        self._player_ramp_btn = QPushButton("Start Ramp")
        self._player_ramp_btn.setEnabled(False)
        self._player_ramp_btn.clicked.connect(self._on_ramp_arb)
        r3.addWidget(self._player_ramp_btn)
        r3.addStretch()
        layout.addLayout(r3)

        # Player plot with column tabs
        try:
            import pyqtgraph as pg
            self._player_tabs = QTabWidget()
            self._player_tabs.setTabPosition(QTabWidget.North)
            self._player_plot_widgets: list = []
            self._player_plot_curves: list = []
            self._player_ramp_curves: list = []
            for i in range(3):
                pw = pg.PlotWidget(background="w")
                pw.setMinimumHeight(200)
                pi = pw.getPlotItem()
                pi.setLabel("left", "Arb output (gain × count)")
                pi.setLabel("bottom", "Sample index")
                pi.showAxis("top")
                pi.getAxis("top").setLabel("Time")
                pi.showGrid(x=True, y=True, alpha=0.25)
                curve = pi.plot(pen=pg.mkPen("#059669", width=1.5),
                                name="Waveform")
                ramp_c = pi.plot(pen=pg.mkPen("#dc2626", width=1.2,
                                              style=Qt.DashLine),
                                 name="Gain envelope")
                self._player_plot_widgets.append(pw)
                self._player_plot_curves.append(curve)
                self._player_ramp_curves.append(ramp_c)
                self._player_tabs.addTab(pw, f"Col {i + 1}")
            layout.addWidget(self._player_tabs, 1)
            self._player_has_plot = True
            for i in range(1, 3):
                self._player_tabs.setTabVisible(i, False)
        except ImportError:
            layout.addWidget(
                QLabel("(install pyqtgraph for waveform display)"))
            self._player_has_plot = False

        self._player_data: list = [None, None, None]
        return grp

    # ------------------------------------------------------------------
    # (Trapping tab moved to procedures/proc_trapping.py — see _build_ui)
    # ------------------------------------------------------------------

    def _build_outputs_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)

        # --- EOM ---
        eom_grp = QGroupBox("EOM")
        eg = QGridLayout()
        r = 0
        r = self._add_reg(eg, r, "EOM_amplitude")
        r = self._add_reg(eg, r, "EOM_threshold")
        r = self._add_reg(eg, r, "EOM_offset")
        r = self._add_reg(eg, r, "EOM_seed")
        r = self._add_reg(eg, r, "Amplitude_sine_EOM")
        r = self._add_reg(eg, r, "eom sine frequency (periods/tick)")
        r = self._add_reg(eg, r, "EOM_amplitude_out")
        r = self._add_reg(eg, r, "EOM reset")
        r = self._add_host(eg, r, "Frequency_sine_EOM (Hz)")
        eom_grp.setLayout(eg)
        layout.addWidget(eom_grp)

        # --- COM output (Cluster) ---
        com_grp = QGroupBox("COM Output (Cluster)")
        cg = QGridLayout()
        r = 0
        r = self._add_reg(cg, r, "Trigger for COM out")
        r = self._add_reg(cg, r, "offset")
        r = self._add_reg(cg, r, "amplitude")
        r = self._add_reg(cg, r, "frequency (periods/tick)")
        r = self._add_reg(cg, r, "duty cycle (periods)")
        r = self._add_host(cg, r, "frequency (kHz)")
        com_grp.setLayout(cg)
        layout.addWidget(com_grp)

        # --- AO channels / Rotation Control ---
        ao_grp = QGroupBox("AO Channels / Rotation Control")
        ag = QGridLayout()
        r = 0
        # Rotation booleans
        r = self._add_reg(ag, r, "Reset voltage")
        r = self._add_reg(ag, r, "If revert AO4 and AO5")
        r = self._add_reg(ag, r, "If scan frequency (AO6 and AO7)?")
        ag.addWidget(QLabel(""), r, 0)  # spacer
        r += 1
        for ch in (4, 5, 6, 7):
            ag.addWidget(QLabel(f"--- AO{ch} ---"), r, 0, 1, 3)
            r += 1
            r = self._add_reg(ag, r, f"Amplitude AO{ch}")
            r = self._add_reg(ag, r, f"phase offset AO{ch}")
            r = self._add_reg(ag, r, f"frequency AO{ch}")
            r = self._add_reg(ag, r, f"reset AO{ch}")
            r = self._add_host(ag, r, f"frequency AO{ch} (Hz)")
        ao_grp.setLayout(ag)
        layout.addWidget(ao_grp)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)
        return widget

    # ------------------------------------------------------------------
    # All Registers tab (raw register view)
    # ------------------------------------------------------------------

    def _build_registers_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)

        for cat in Category:
            grp = QGroupBox(cat.value)
            grp.setCheckable(True)
            grp.setChecked(True)
            gl = QGridLayout()
            r = 0
            for name in names_by_category(cat):
                r = self._add_reg(gl, r, name)
            grp.setLayout(gl)
            layout.addWidget(grp)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)
        return widget

    # ------------------------------------------------------------------
    # Monitor tab
    # ------------------------------------------------------------------

    def _build_plot_tab(self) -> QWidget:
        widget = QWidget()
        self._plot_tab_layout = QVBoxLayout(widget)
        self._plot_tab_layout.setContentsMargins(4, 4, 4, 4)

        # Settings / toolbar row
        top = QHBoxLayout()
        top.addWidget(QLabel("Plot poll interval:"))
        self._plot_poll_spin = QSpinBox()
        self._plot_poll_spin.setRange(5, 1000)
        self._plot_poll_spin.setValue(self._ctrl.config.plot_interval_ms)
        self._plot_poll_spin.setSuffix(" ms")
        self._plot_poll_spin.valueChanged.connect(self._on_plot_poll_changed)
        top.addWidget(self._plot_poll_spin)
        top.addStretch()

        self._popout_btn = QPushButton("⬡  Pop Out")
        self._popout_btn.setFixedWidth(100)
        self._popout_btn.setToolTip("Undock the plot grid into a separate window")
        self._popout_btn.clicked.connect(self._on_plot_popout)
        top.addWidget(self._popout_btn)

        self._plot_tab_layout.addLayout(top)

        # Embedded 3×3 plot grid
        self._plot_widget = FPGAPlotWidget()
        self._plot_tab_layout.addWidget(self._plot_widget, stretch=1)

        # Placeholder shown when the plot is undocked
        self._plot_placeholder = QWidget()
        ph_layout = QVBoxLayout(self._plot_placeholder)
        ph_lbl = QLabel("Monitor plots are in a separate window.")
        ph_lbl.setAlignment(Qt.AlignCenter)
        ph_lbl.setStyleSheet("color: gray; font-size: 13px;")
        ph_layout.addStretch()
        ph_layout.addWidget(ph_lbl)
        dock_btn = QPushButton("⬡  Dock")
        dock_btn.setFixedWidth(90)
        dock_btn.clicked.connect(self._on_plot_dockin)
        ph_layout.addWidget(dock_btn, alignment=Qt.AlignHCenter)
        ph_layout.addStretch()
        self._plot_placeholder.hide()

        self._plot_popout_window: QMainWindow | None = None

        return widget

    def _on_plot_popout(self) -> None:
        """Move the plot widget into a standalone window."""
        if self._plot_popout_window is not None:
            self._plot_popout_window.raise_()
            return

        # Detach plot from the tab layout
        self._plot_tab_layout.removeWidget(self._plot_widget)
        self._plot_widget.setParent(None)

        # Show placeholder
        self._plot_tab_layout.addWidget(self._plot_placeholder, stretch=1)
        self._plot_placeholder.show()
        self._popout_btn.setEnabled(False)

        # Create standalone window
        win = QMainWindow()
        win.setWindowTitle("usphere — Monitor Plots")
        win.resize(1280, 720)
        icon_path = Path(__file__).parent / "assets" / "Logo_transparent_outlined.PNG"
        if icon_path.exists():
            win.setWindowIcon(QIcon(str(icon_path)))
        win.setCentralWidget(self._plot_widget)
        self._plot_widget.show()
        win.show()
        self._plot_popout_window = win

        def _on_win_close(event):
            self._on_plot_dockin()
            event.accept()

        win.closeEvent = _on_win_close

    def _on_plot_dockin(self) -> None:
        """Return the plot widget from the standalone window back to the tab."""
        if self._plot_popout_window is None:
            return

        # Detach from the window without destroying
        self._plot_widget.setParent(None)

        # Remove placeholder
        self._plot_tab_layout.removeWidget(self._plot_placeholder)
        self._plot_placeholder.hide()

        # Re-embed in the tab
        self._plot_tab_layout.addWidget(self._plot_widget, stretch=1)
        self._plot_widget.show()
        self._popout_btn.setEnabled(True)

        # Close the pop-out window (suppress recursive closeEvent)
        win = self._plot_popout_window
        self._plot_popout_window = None
        win.closeEvent = lambda e: e.accept()
        win.close()

    # ==================================================================
    # Widget-building helpers
    # ==================================================================

    def _add_reg(self, grid: QGridLayout, row: int, name: str) -> int:
        """Add one FPGA register row to *grid*. Returns next row index."""
        reg = REGISTER_MAP.get(name)
        if reg is None:
            return row
        grid.addWidget(QLabel(name), row, 0)
        ro = (reg.access == Access.READ)
        w = 50 if reg.is_bool else 100
        edit = _make_edit(0.0, readonly=ro, width=w)
        self._reg_edits.setdefault(name, edit)
        grid.addWidget(edit, row, 1)
        if not ro:
            btn = QPushButton("Set")
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _, n=name, e=edit: self._write_one_edit(n, e))
            grid.addWidget(btn, row, 2)
            # Grey live-value label: shows the current FPGA value from the monitor
            # without disturbing the editable input field above.
            live_lbl = QLabel("—")
            live_lbl.setStyleSheet(
                "color: #9ca3af; font-size: 10px; font-style: italic;")
            live_lbl.setFixedWidth(90)
            live_lbl.setToolTip("Current FPGA value (updated by monitor)")
            self._reg_live_labels.setdefault(name, live_lbl)
            grid.addWidget(live_lbl, row, 3)
        return row + 1

    def _add_host(self, grid: QGridLayout, row: int, name: str) -> int:
        """Add one host-param row to *grid*. Returns next row index."""
        hp = HOST_PARAM_MAP.get(name)
        if hp is None:
            return row
        grid.addWidget(QLabel(name), row, 0)
        default = self._host_values.get(name, hp.default)
        edit = _make_edit(default, width=100)
        edit.setToolTip(hp.description)
        self._host_edits[name] = edit
        grid.addWidget(edit, row, 1)
        return row + 1

    # ==================================================================
    # Session persistence
    # ==================================================================

    def _restore_full_state(self) -> None:
        """Load session_state.json and restore all GUI widget values on startup."""
        state = _load_session_state()
        if not state:
            return
        self._session_state = state
        try:
            cfg = state.get("config", {})
            if "bitfile" in cfg:
                self._bitfile_edit.setText(cfg["bitfile"])
            if "resource" in cfg:
                self._resource_edit.setText(cfg["resource"])
            if "poll_interval_ms" in cfg:
                self._poll_spin.setValue(int(cfg["poll_interval_ms"]))
            for name, val in state.get("host_params", {}).items():
                self._host_values[name] = float(val)
                edit = self._host_edits.get(name)
                if edit is not None:
                    edit.setText(_fmt(float(val)))
            # Resources: support both new format and legacy {dropper/shaker/trapping} keys
            resources_data = state.get("resources", {
                "Dropper Stage": state.get("dropper", {}),
                "Shake Dropper": state.get("shaker", {}),
                "Trapping":      state.get("trapping", {}),
            })
            self._resources.restore_ui_states(resources_data)
        except Exception as exc:
            self._append_status(f"Session restore warning: {exc}")

    def _restore_registers_from_state(self) -> None:
        """Write FPGA registers from session_state.json (called after connect)."""
        regs = self._session_state.get("registers", {})
        if not regs:
            return
        to_write = {k: v for k, v in regs.items()
                    if k in {r.name for r in writable_registers()}}
        if to_write:
            errors = self._ctrl.write_many(to_write)
            ok = len(to_write) - len(errors)
            self._append_status(f"Session: restored {ok}/{len(to_write)} FPGA registers")

    def _gather_full_state(self) -> dict:
        """Collect all GUI state into a dict for session_state.json."""
        state: dict = {}
        state["config"] = self._current_config().to_dict()
        state["host_params"] = {k: float(v) for k, v in self._host_values.items()}
        if self._last_register_values:
            state["registers"] = dict(self._last_register_values)
        state["resources"] = self._resources.get_ui_states()
        return state

    def _autosave_state(self) -> None:
        """Periodic + on-close autosave of all GUI state to session_state.json."""
        try:
            _save_session_state(self._gather_full_state())
        except Exception:
            pass

    def _current_config(self) -> FPGAConfig:
        return FPGAConfig(
            bitfile=self._bitfile_edit.text().strip(),
            resource=self._resource_edit.text().strip(),
            poll_interval_ms=self._poll_spin.value(),
        )

    def _gather_host_params(self) -> dict[str, float]:
        """Read current host-param values from GUI widgets."""
        for name, edit in self._host_edits.items():
            self._host_values[name] = _float(edit)
        return dict(self._host_values)

    # ==================================================================
    # Connection slots
    # ==================================================================

    def _browse_bitfile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select FPGA Bitfile",
            str(Path(self._bitfile_edit.text()).parent),
            "FPGA Bitfiles (*.lvbitx);;All files (*)",
        )
        if path:
            self._bitfile_edit.setText(path)

    def _on_connect_clicked(self) -> None:
        cfg = self._current_config()
        self._ctrl.config = cfg
        try:
            self._ctrl.connect()
        except Exception as exc:
            self._append_status(f"Connection failed: {exc}")

    def _on_disconnect_clicked(self) -> None:
        self._ctrl.disconnect()

    def _on_connected(self) -> None:
        self._conn_label.setText(
            "Connected (SIM)" if self._ctrl.is_simulated else "Connected")
        self._conn_label.setStyleSheet("color: green; font-weight: bold;")
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._bitfile_edit.setReadOnly(True)
        self._resource_edit.setReadOnly(True)
        self._restore_registers_from_state()
        self._on_read_all()

    def _on_disconnected(self) -> None:
        self._conn_label.setText("Disconnected")
        self._conn_label.setStyleSheet("color: red; font-weight: bold;")
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._bitfile_edit.setReadOnly(False)
        self._resource_edit.setReadOnly(False)

    # ==================================================================
    # Register / host-param operations
    # ==================================================================

    def _write_one(self, name: str) -> None:
        """Write a single register from its edit widget."""
        edit = self._reg_edits.get(name)
        if edit is None:
            return
        self._write_one_edit(name, edit)

    def _write_one_edit(self, name: str, edit: QLineEdit) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        value = _float(edit)
        try:
            self._ctrl.write_register(name, value)
            self._append_status(f"Wrote {name} = {value}")
        except Exception as exc:
            self._append_status(f"Write failed ({name}): {exc}")

    def _write_one_value(self, name: str, value: float) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        try:
            self._ctrl.write_register(name, value)
            self._append_status(f"Wrote {name} = {value}")
        except Exception as exc:
            self._append_status(f"Write failed ({name}): {exc}")

    def _on_read_all(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        values = self._ctrl.read_all()
        self._update_reg_edits(values, initial=True)
        self._append_status(f"Read {len(values)} registers")

    def _on_registers_updated(self, values: dict) -> None:
        self._last_register_values = values
        self._update_reg_edits(values)
        self._resources.notify_fpga_update(values)

    def _on_plot_data(self, values: dict) -> None:
        self._plot_widget.push_values(values)
        self._resources.notify_fast_data(values)

    def _update_reg_edits(self, values: dict[str, float],
                          initial: bool = False) -> None:
        """Update register display widgets from *values*.

        Parameters
        ----------
        values  : {register_name: float} snapshot from the FPGA.
        initial : True when called from a manual "Read All" or on connect.
                  Populates the editable input fields with the current FPGA
                  value so the operator starts with the right number.
                  False (default) during live monitor updates: writable fields
                  are intentionally left alone so the operator can type without
                  being interrupted; only the small grey live-value labels are
                  updated instead.
        """
        for name, val in values.items():
            edit = self._reg_edits.get(name)
            if edit is None:
                continue
            reg = REGISTER_MAP.get(name)
            is_writable = reg is not None and reg.access != Access.READ
            is_int = reg is not None and reg.is_integer
            if is_writable:
                # Always refresh the grey live-value label
                live_lbl = self._reg_live_labels.get(name)
                if live_lbl is not None:
                    live_lbl.setText(_fmt(val, is_int))
                # Only push into the editable field on startup / explicit read
                if initial:
                    edit.setText(_fmt(val, is_int))
            else:
                # Read-only indicators: always update
                edit.setText(_fmt(val, is_int))

    # ==================================================================
    # Change pars
    # ==================================================================

    def _on_change_pars(self, axis: str) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        host = self._gather_host_params()
        # Also collect direct PID register values from the GUI
        a = axis.upper()
        al = axis.lower()
        pid_regs = [
            f"pg {a}", f"dg {a}", f"dg band {a}",
            f"pg {a} before", f"dg {a} before", f"dg band {a} before",
            f"Upper lim {a}", f"Lower lim {a}",
            f"Upper lim {a} before", f"Lower lim {a} before",
            f"{a} Setpoint", f"DC offset {a}",
            f"{a} before Setpoint", f"Use {a} PID before",
        ]
        pid_regs += [f" ig {a}", f" ig {a} before"]
        pid_values = {}
        for name in pid_regs:
            edit = self._reg_edits.get(name)
            if edit is not None:
                pid_values[name] = _float(edit)

        errors = self._ctrl.change_pars(axis, host, pid_values)
        if errors:
            self._append_status(f"Change pars ({axis}) errors: {errors}")
        # Re-read to see updated coefficients
        self._on_read_all()

    # ==================================================================
    # Boost — temporarily multiply gains
    # ==================================================================

    def _on_boost(self, axis: str) -> None:
        """Multiply proportional and derivative gains by the boost factor and write."""
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        a = axis.upper()
        al = axis.lower()
        mult = self._boost_multiplier

        gain_regs = [f"pg {a}", f"dg {a}", f"dg band {a}"]
        boosted: dict[str, float] = {}
        for name in gain_regs:
            edit = self._reg_edits.get(name)
            if edit is not None:
                boosted[name] = _float(edit) * mult

        errors = self._ctrl.write_many(boosted)
        if errors:
            self._append_status(f"Boost ({axis}) errors: {errors}")
        else:
            self._append_status(
                f"Boost ({axis}): gains multiplied by {mult}")
        self._on_read_all()

    # ==================================================================
    # Ramp operations
    # ==================================================================

    def _on_ramp_power(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        host = self._gather_host_params()
        target = host.get("End value power", 0)
        step = host.get("Step power", 0)
        delay = host.get("Delay Time (s) power", 0.05)
        if step <= 0:
            self._append_status("Step power must be > 0")
            return
        self._ctrl.ramp_register("DC offset Z", target, step, delay)
        self._append_status(f"Ramping DC offset Z → {target}")

    def _on_ramp_arb(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        try:
            delay = self._player_ramp_delay.value()
            end   = self._player_ramp_end.value()
            step  = self._player_ramp_step.value()
            start = self._player_gain0_spin.value()
            if step > 0:
                self._ctrl.ramp_register("Arb gain (ch0)", end, step, delay)
                self._append_status(
                    f"Ramping Arb gain (ch0): {start:.2f} → {end:.2f} "
                    f"step={step:.3f} delay={delay:.3f} s")
        except Exception as exc:
            self._append_status(f"Ramp arb error: {exc}")

    # ==================================================================
    # Waveform designer handlers
    # ==================================================================

    def _on_wd_comb_mode_changed(self, idx: int) -> None:
        self._wd_comb_stack.setCurrentIndex(idx)

    def _on_wd_trap_frac_changed(self) -> None:
        try:
            r = float(self._wd_trap_rise.text() or "0")
            h = float(self._wd_trap_high.text() or "0")
            f = float(self._wd_trap_fall.text() or "0")
            low = 1.0 - r - h - f
            self._wd_trap_low_lbl.setText(f"Low:{low:.3f}")
            self._wd_trap_low_lbl.setStyleSheet(
                "color: red; font-size: 10px;" if low < -0.001
                else "color: gray; font-size: 10px;")
        except ValueError:
            self._wd_trap_low_lbl.setText("Low:—")

    def _on_wd_params_changed(self, *_) -> None:
        """Update derived frequency labels when points/sample-rate/cycles change."""
        try:
            n_points = int(self._wd_npoints_combo.currentText())
            sample_rate = float(self._wd_samplerate_edit.text() or "1e6")
        except ValueError:
            return
        for ncycles_spin, freq_lbl in [
            (self._wd_sine_ncycles,  self._wd_sine_freq_lbl),
            (self._wd_tri_ncycles,   self._wd_tri_freq_lbl),
            (self._wd_trap_ncycles,  self._wd_trap_freq_lbl),
        ]:
            n_cyc = ncycles_spin.value()
            if n_points > 0 and sample_rate > 0:
                freq = sample_rate * n_cyc / n_points
                if freq >= 1e6:
                    freq_lbl.setText(f"→ {freq / 1e6:.3f} MHz")
                elif freq >= 1e3:
                    freq_lbl.setText(f"→ {freq / 1e3:.3f} kHz")
                else:
                    freq_lbl.setText(f"→ {freq:.3f} Hz")
            else:
                freq_lbl.setText("")

    def _on_wd_resolution_changed(self, bits: int) -> None:
        max_count = 2 ** (bits - 1) - 1
        self._wd_maxcount_lbl.setText(f"max ±{max_count}")
        self._wd_amplitude_spin.setRange(1, max_count)
        self._wd_amplitude_spin.setValue(
            min(self._wd_amplitude_spin.value(), max_count))
        self._wd_dc_spin.setRange(-max_count, max_count)

    def _on_wd_ncols_changed(self, n: int) -> None:
        for i, btn in enumerate(self._wd_col_btns):
            btn.setVisible(i < n)
        if self._wd_active_col >= n:
            self._on_wd_col_btn_clicked(0)

    def _on_wd_col_btn_clicked(self, idx: int) -> None:
        self._wd_active_col = idx
        for i, btn in enumerate(self._wd_col_btns):
            btn.setChecked(i == idx)

    def _wd_get_int_samples(self, normalized: "np.ndarray") -> "np.ndarray":
        """Convert normalized [-1,+1] float array to clipped integer DAC counts."""
        bits = self._wd_bits_spin.value()
        max_count = 2 ** (bits - 1) - 1
        amp = self._wd_amplitude_spin.value()
        dc = self._wd_dc_spin.value()
        raw = np.round(normalized * amp + dc).astype(np.int32)
        return np.clip(raw, -max_count, max_count)

    def _wd_update_top_axis(self, n_points: int, sample_rate: float) -> None:
        """Set time-labelled ticks on the top x-axis of the generator preview."""
        if not self._wd_has_plot or n_points <= 0 or sample_rate <= 0:
            return
        import pyqtgraph as pg
        pi = self._wd_plot_widget.getPlotItem()
        ax = pi.getAxis("top")
        tick_count = 5
        positions = [int(n_points * i / tick_count) for i in range(tick_count + 1)]

        def _fmt(t: float) -> str:
            if t < 1e-6:
                return f"{t * 1e9:.1f} ns"
            if t < 1e-3:
                return f"{t * 1e6:.1f} µs"
            if t < 1:
                return f"{t * 1e3:.2f} ms"
            return f"{t:.3f} s"

        labels = [_fmt(p / sample_rate) for p in positions]
        ax.setTicks([list(zip(positions, labels))])

    def _on_wd_generate(self) -> None:
        try:
            n_points = int(self._wd_npoints_combo.currentText())
        except ValueError:
            self._append_status("Invalid n_points.")
            return

        tab_idx = self._wd_type_tabs.currentIndex()
        try:
            sample_rate = float(self._wd_samplerate_edit.text() or "1e6")

            if tab_idx == 0:        # Sine
                n_cyc = self._wd_sine_ncycles.value()
                phase = float(self._wd_sine_phase.text() or "0.0")
                result = generate_sine(n_points, n_cyc, phase)

            elif tab_idx == 1:      # Triangle
                n_cyc = self._wd_tri_ncycles.value()
                sym = float(self._wd_tri_symmetry.text() or "0.5")
                result = generate_triangle(n_points, n_cyc, sym)

            elif tab_idx == 2:      # Trapezoid
                n_cyc = self._wd_trap_ncycles.value()
                rise = float(self._wd_trap_rise.text() or "0.1")
                high = float(self._wd_trap_high.text() or "0.4")
                fall = float(self._wd_trap_fall.text() or "0.1")
                result = generate_trapezoid(n_points, n_cyc, rise, high, fall)

            elif tab_idx == 3:      # Freq Comb (MC)
                mode = self._wd_comb_mode.currentIndex()
                if mode == 0:
                    freqs = [float(x.strip())
                             for x in self._wd_comb_list_edit.text().split(",")
                             if x.strip()]
                elif mode == 1:
                    start = float(self._wd_comb_arange_start.text())
                    stop  = float(self._wd_comb_arange_stop.text())
                    step  = float(self._wd_comb_arange_step.text())
                    freqs = list(np.arange(start, stop + step / 2, step))
                else:
                    trap_f = float(self._wd_comb_trap_freq.text())
                    fracs  = [float(x.strip())
                              for x in self._wd_comb_fracs_edit.text().split(",")
                              if x.strip()]
                    freqs = [trap_f * f for f in fracs]
                if not freqs:
                    self._append_status("Freq comb: no frequencies specified.")
                    return
                n_trials = int(self._wd_comb_trials.text() or "1000")
                self._wd_mc_stop_flag = [False]
                self._wd_mc_worker = _CombMCWorker(
                    n_points, sample_rate, freqs, n_trials, self._wd_mc_stop_flag)
                self._wd_mc_worker.progress.connect(self._on_wd_mc_progress)
                self._wd_mc_worker.finished.connect(self._on_wd_mc_done)
                self._wd_gen_btn.setEnabled(False)
                self._wd_cancel_btn.show()
                self._wd_mc_progress.setValue(0)
                self._wd_mc_progress.show()
                self._wd_mc_worker.start()
                return
            else:
                return

            int_samples = self._wd_get_int_samples(result.samples)
            self._wd_columns[self._wd_active_col] = int_samples.copy()
            self._wd_result = result
            self._on_wd_update_preview(int_samples, result, n_points, sample_rate)
            self._wd_desc_lbl.setText(
                f"Col {self._wd_active_col + 1} stored  |  {result.description}")

        except Exception as exc:
            self._append_status(f"Waveform generate error: {exc}")

    def _on_wd_cancel(self) -> None:
        self._wd_mc_stop_flag[0] = True

    def _on_wd_mc_progress(self, trial: int, total: int,
                            best_rms: float) -> None:
        pct = int(100 * trial / total) if total > 0 else 100
        self._wd_mc_progress.setValue(pct)
        self._wd_desc_lbl.setText(
            f"MC trial {trial}/{total}  best RMS/peak={best_rms:.4f}")

    def _on_wd_mc_done(self, result: object) -> None:
        self._wd_mc_progress.hide()
        self._wd_cancel_btn.hide()
        self._wd_gen_btn.setEnabled(True)
        self._wd_mc_worker = None
        try:
            n_points = int(self._wd_npoints_combo.currentText())
            sample_rate = float(self._wd_samplerate_edit.text() or "1e6")
        except ValueError:
            return
        int_samples = self._wd_get_int_samples(result.samples)
        self._wd_columns[self._wd_active_col] = int_samples.copy()
        self._wd_result = result
        self._on_wd_update_preview(int_samples, result, n_points, sample_rate)
        self._wd_desc_lbl.setText(
            f"Col {self._wd_active_col + 1} stored  |  {result.description}")

    def _on_wd_update_preview(self, int_samples, result,
                               n_points: int, sample_rate: float) -> None:
        if not self._wd_has_plot:
            return
        x = np.arange(len(int_samples))
        self._wd_plot_curve.setData(x, int_samples)
        self._wd_update_top_axis(n_points, sample_rate)

        # Annotation: f and Vpp in physical units
        try:
            tab_idx = self._wd_type_tabs.currentIndex()
            if tab_idx == 0:
                n_cyc = self._wd_sine_ncycles.value()
            elif tab_idx == 1:
                n_cyc = self._wd_tri_ncycles.value()
            elif tab_idx == 2:
                n_cyc = self._wd_trap_ncycles.value()
            else:
                n_cyc = 1
            freq_hz = sample_rate * n_cyc / n_points if n_points > 0 else 0
            if freq_hz >= 1e6:
                freq_str = f"{freq_hz / 1e6:.4f} MHz"
            elif freq_hz >= 1e3:
                freq_str = f"{freq_hz / 1e3:.3f} kHz"
            else:
                freq_str = f"{freq_hz:.3f} Hz"

            cal_cts = float(self._wd_volt_cal_counts.text() or "2047")
            cal_v   = float(self._wd_volt_cal_v.text() or "10")
            amp_cts = self._wd_amplitude_spin.value()
            dc_cts  = self._wd_dc_spin.value()
            vpp_v   = (amp_cts * 2 * cal_v / cal_cts) if cal_cts != 0 else 0
            dc_v    = (dc_cts * cal_v / cal_cts) if cal_cts != 0 else 0
            ann = (f"f = {freq_str}   "
                   f"Vpp = {vpp_v:.3f} V ({amp_cts * 2} cts)   "
                   f"DC = {dc_v:.3f} V ({dc_cts} cts)")
            self._wd_annot.setText(ann)
            pi = self._wd_plot_widget.getPlotItem()
            vr = pi.viewRange()
            self._wd_annot.setPos(vr[0][0], vr[1][1])
        except Exception:
            pass

    def _on_wd_save(self) -> None:
        """Write all active columns to a single integer CSV (no header)."""
        ncols = self._wd_ncols_spin.value()
        cols = self._wd_columns[:ncols]
        if all(c is None for c in cols):
            self._append_status("No waveform columns generated yet.")
            return
        default_dir = Path(__file__).parent / "waveforms"
        default_dir.mkdir(exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Waveform CSV",
            str(default_dir / "waveform.csv"),
            "CSV files (*.csv);;Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            n_points = int(self._wd_npoints_combo.currentText())
            arrays = []
            for c in cols:
                arrays.append(c[:n_points] if c is not None
                               else np.zeros(n_points, dtype=np.int32))
            data = (np.column_stack(arrays) if len(arrays) > 1
                    else arrays[0].reshape(-1, 1))
            np.savetxt(path, data, fmt="%d", delimiter=",")
            self._arb_file_edit.setText(path)
            self._append_status(
                f"Saved {n_points} rows × {ncols} col(s) → {path}")
        except Exception as exc:
            self._append_status(f"Save error: {exc}")

    # ==================================================================
    # Waveform player handlers
    # ==================================================================

    def _on_player_load(self) -> None:
        path = self._arb_file_edit.text().strip()
        if not path:
            self._append_status("No file specified.")
            return
        try:
            raw = np.loadtxt(path, delimiter=",", ndmin=2)
            if raw.ndim == 1:
                raw = raw.reshape(-1, 1)
            ncols = min(raw.shape[1], 3)
            for i in range(3):
                self._player_data[i] = raw[:, i].astype(float) if i < ncols else None
                if self._player_has_plot:
                    self._player_tabs.setTabVisible(i, i < ncols)
            self._on_player_display_changed()   # also calls _player_update_time_axes
            self._append_status(
                f"Loaded {raw.shape[0]} pts × {ncols} col(s) from {path}")
        except Exception as exc:
            self._append_status(f"Load error: {exc}")

    def _on_player_display_changed(self, *_) -> None:
        if not self._player_has_plot:
            return
        gain = self._player_gain0_spin.value()
        for data, curve in zip(self._player_data, self._player_plot_curves):
            if data is not None:
                curve.setData(np.arange(len(data)), data * gain)
            else:
                curve.setData([], [])
        self._on_player_update_ramp_preview()
        self._player_update_time_axes()

    def _player_update_time_axes(self) -> None:
        """Update top (time) axis on all player plots based on Count(uSec)."""
        if not self._player_has_plot:
            return
        count_usec = self._player_count_usec_spin.value()
        sample_period_s = count_usec * 1e-6

        def _fmt_t(t: float) -> str:
            if t < 1e-6:
                return f"{t * 1e9:.1f} ns"
            if t < 1e-3:
                return f"{t * 1e6:.1f} µs"
            if t < 1.0:
                return f"{t * 1e3:.2f} ms"
            return f"{t:.3f} s"

        for i, (pw, data) in enumerate(zip(self._player_plot_widgets,
                                           self._player_data)):
            if data is None:
                continue
            n = len(data)
            if n == 0:
                continue
            ax = pw.getPlotItem().getAxis("top")
            tick_count = 6
            positions = [int(n * j / tick_count) for j in range(tick_count + 1)]
            labels = [_fmt_t(p * sample_period_s) for p in positions]
            ax.setTicks([list(zip(positions, labels))])

    def _on_player_time_axis_changed(self, *_) -> None:
        self._player_update_time_axes()

    def _on_player_ramp_toggled(self, checked: bool) -> None:
        self._player_ramp_end.setEnabled(checked)
        self._player_ramp_step.setEnabled(checked)
        self._player_ramp_delay.setEnabled(checked)
        self._player_ramp_btn.setEnabled(checked)
        self._on_player_update_ramp_preview()

    def _on_player_update_ramp_preview(self, *_) -> None:
        if not self._player_has_plot:
            return
        if not self._player_ramp_cb.isChecked():
            for curve in self._player_ramp_curves:
                curve.setData([], [])
            return
        try:
            start_gain = self._player_gain0_spin.value()
            end_gain   = self._player_ramp_end.value()
            step_gain  = self._player_ramp_step.value()
            if step_gain <= 0:
                return
            n_steps = max(1, round(abs(end_gain - start_gain) / step_gain)) + 1
            gain_vals = np.linspace(start_gain, end_gain, n_steps)
            col0 = self._player_data[0]
            n_buf = len(col0) if col0 is not None and len(col0) > 0 else 1
            # Envelope in same units as display: gain * waveform_max
            # Use gain as the amplitude scale indicator (staircase)
            env_x = np.repeat(np.arange(n_steps) * n_buf, 2)[1:-1]
            env_y = np.repeat(gain_vals, 2)[1:-1]
            for curve in self._player_ramp_curves:
                curve.setData(env_x, env_y)
        except Exception:
            pass

    def _on_player_set_gains(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        try:
            for reg, sp in [
                ("Arb gain (ch0)", self._player_gain0_spin),
                ("Arb gain (ch1)", self._player_gain1_spin),
                ("Arb gain (ch2)", self._player_gain2_spin),
            ]:
                self._ctrl.write_register(reg, sp.value())
            self._append_status("Arb gains written to FPGA.")
        except Exception as exc:
            self._append_status(f"Set gain error: {exc}")

    # ==================================================================
    # Arb waveform
    # ==================================================================

    def _browse_arb_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Waveform File",
            str(Path(__file__).parent),
            "Text files (*.txt *.csv *.dat);;All files (*)",
        )
        if path:
            self._arb_file_edit.setText(path)

    def _on_write_arb_buffers(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        filepath = self._arb_file_edit.text().strip()
        if not filepath:
            self._append_status("No arb waveform file selected.")
            return
        try:
            self._ctrl.load_arb_waveform(filepath)
        except Exception as exc:
            self._append_status(f"Arb load failed: {exc}")

    # ==================================================================
    # Sphere save / load
    # ==================================================================

    def _on_save_sphere(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        default_dir = Path(__file__).parent / "data"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Sphere Parameters",
            str(default_dir / f"sphere_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            host = self._gather_host_params()
            self._ctrl.save_sphere(path, host)

    def _on_load_sphere(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Sphere Parameters",
            str(Path(__file__).parent / "data"),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            errors, host = self._ctrl.load_sphere(path)
            if errors:
                self._append_status(f"Load errors: {errors}")
            # Restore host params to GUI
            for name, val in host.items():
                self._host_values[name] = val
                edit = self._host_edits.get(name)
                if edit is not None:
                    edit.setText(_fmt(val))
            self._on_read_all()

    # ==================================================================
    # Monitor
    # ==================================================================

    def _on_start_monitor(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        self._ctrl.config.poll_interval_ms = self._poll_spin.value()
        self._ctrl.config.plot_interval_ms = self._plot_poll_spin.value()
        self._ctrl.stop_monitor()          # restart so new intervals take effect
        self._ctrl.start_monitor(plot_names=ALL_PLOT_NAMES)

    def _on_plot_poll_changed(self, value: int) -> None:
        """Live-apply plot interval change; restart monitor if running."""
        self._ctrl.config.plot_interval_ms = value
        if self._ctrl._monitor_thread is not None and \
                self._ctrl._monitor_thread.is_alive():
            self._ctrl.stop_monitor()
            self._ctrl.start_monitor(plot_names=ALL_PLOT_NAMES)

    def _on_stop_monitor(self) -> None:
        self._ctrl.stop_monitor()

    # ==================================================================
    # Snapshots
    # ==================================================================

    def _on_save_snapshot(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        default_dir = Path(__file__).parent / "data"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Register Snapshot",
            str(default_dir / f"fpga_snapshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            self._ctrl.save_snapshot(path)

    def _on_load_snapshot(self) -> None:
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Register Snapshot",
            str(Path(__file__).parent / "data"),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            errors = self._ctrl.load_snapshot(path)
            if errors:
                self._append_status(f"Load errors: {errors}")
            self._on_read_all()

    # ==================================================================
    # Status log
    # ==================================================================

    def _append_status(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_text.append(f"[{ts}] {msg}")

    # ==================================================================
    # Vacuum tab — Edwards TIC
    # ==================================================================

    def _build_tic_tab(self) -> QWidget:
        widget  = QWidget()
        outer   = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)
        tic_row = QHBoxLayout()
        tic_row.setSpacing(10)

        # ---- Left column: connection + pump control ----
        left = QVBoxLayout()

        # Connection group
        conn_grp = QGroupBox("Connection")
        cg = QGridLayout()
        cg.addWidget(QLabel("Port:"), 0, 0)
        self._tic_port_edit = QLineEdit("COM3")
        self._tic_port_edit.setFixedWidth(80)
        cg.addWidget(self._tic_port_edit, 0, 1)
        cg.addWidget(QLabel("Baud:"), 1, 0)
        self._tic_baud_edit = QLineEdit("9600")
        self._tic_baud_edit.setFixedWidth(80)
        cg.addWidget(self._tic_baud_edit, 1, 1)

        btn_row = QHBoxLayout()
        self._tic_connect_btn = QPushButton("Connect")
        self._tic_connect_btn.clicked.connect(self._on_tic_connect)
        btn_row.addWidget(self._tic_connect_btn)
        self._tic_disconnect_btn = QPushButton("Disconnect")
        self._tic_disconnect_btn.setEnabled(False)
        self._tic_disconnect_btn.clicked.connect(self._on_tic_disconnect)
        btn_row.addWidget(self._tic_disconnect_btn)
        cg.addLayout(btn_row, 2, 0, 1, 2)

        self._tic_conn_lbl = QLabel("Disconnected")
        self._tic_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        cg.addWidget(self._tic_conn_lbl, 3, 0, 1, 2)
        conn_grp.setLayout(cg)
        left.addWidget(conn_grp)

        # Auto-poll group
        poll_grp = QGroupBox("Auto Poll")
        pg = QHBoxLayout()
        self._tic_autopoll_cb = QCheckBox("Enable")
        self._tic_autopoll_cb.setChecked(True)
        self._tic_autopoll_cb.toggled.connect(self._on_tic_autopoll_toggled)
        pg.addWidget(self._tic_autopoll_cb)
        pg.addWidget(QLabel("Interval:"))
        self._tic_interval_spin = QSpinBox()
        self._tic_interval_spin.setRange(500, 30000)
        self._tic_interval_spin.setValue(2000)
        self._tic_interval_spin.setSuffix(" ms")
        self._tic_interval_spin.valueChanged.connect(
            lambda v: self._tic_timer.setInterval(v)
                      if self._tic_timer.isActive() else None)
        pg.addWidget(self._tic_interval_spin)
        poll_btn = QPushButton("Read Now")
        poll_btn.clicked.connect(self._on_tic_poll)
        pg.addWidget(poll_btn)
        pg.addStretch()
        poll_grp.setLayout(pg)
        left.addWidget(poll_grp)

        # Pump control group
        pump_ctrl_grp = QGroupBox("Pump Control")
        pc = QVBoxLayout()

        start_stop_row = QHBoxLayout()
        self._tic_start_btn = QPushButton("▶  Start Pump")
        self._tic_start_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; "
            "font-weight: bold; padding: 6px 14px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #22c55e; }"
            "QPushButton:disabled { background-color: #86efac; }")
        self._tic_start_btn.setEnabled(False)
        self._tic_start_btn.clicked.connect(self._on_tic_start_pump)
        start_stop_row.addWidget(self._tic_start_btn)

        self._tic_stop_btn = QPushButton("■  Stop Pump")
        self._tic_stop_btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-weight: bold; padding: 6px 14px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #ef4444; }"
            "QPushButton:disabled { background-color: #fca5a5; }")
        self._tic_stop_btn.setEnabled(False)
        self._tic_stop_btn.clicked.connect(self._on_tic_stop_pump)
        start_stop_row.addWidget(self._tic_stop_btn)
        pc.addLayout(start_stop_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed setpoint (%):"))
        self._tic_speed_edit = QLineEdit("0")
        self._tic_speed_edit.setFixedWidth(50)
        self._tic_speed_edit.setToolTip("0 = default full speed")
        speed_row.addWidget(self._tic_speed_edit)
        speed_set_btn = QPushButton("Set")
        speed_set_btn.setFixedWidth(40)
        speed_set_btn.clicked.connect(self._on_tic_set_speed)
        speed_row.addWidget(speed_set_btn)
        speed_row.addStretch()
        pc.addLayout(speed_row)

        pump_ctrl_grp.setLayout(pc)
        left.addWidget(pump_ctrl_grp)
        left.addStretch()

        tic_row.addLayout(left, 1)

        # ---- Right column: gauges + telemetry ----
        right = QVBoxLayout()

        # Pressure gauges
        gauge_grp = QGroupBox("Pressure Gauges")
        gg = QVBoxLayout()

        for label_text, attr in (("WRG (wide-range gauge)", "_tic_wrg_lbl"),
                                  ("APGX (Pirani gauge)",   "_tic_apgx_lbl")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text + ":"))
            lbl = QLabel("—")
            lbl.setFont(QFont("Consolas", 16, QFont.Bold))
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setFixedWidth(200)
            lbl.setStyleSheet(
                "background: #f9fafb; border: 1px solid #d1d5db; "
                "border-radius: 4px; padding: 4px 8px;")
            setattr(self, attr, lbl)
            row.addWidget(lbl, 1)
            gg.addLayout(row)

        gauge_grp.setLayout(gg)
        right.addWidget(gauge_grp)

        # Pump telemetry
        tel_grp = QGroupBox("Pump Telemetry")
        tg = QGridLayout()
        r = 0
        tel_fields = [
            ("Status",  "_tic_tel_status"),
            ("Speed",   "_tic_tel_speed"),
            ("Power",   "_tic_tel_power"),
            ("Current", "_tic_tel_current"),
            ("Voltage", "_tic_tel_voltage"),
            ("Temp",    "_tic_tel_temp"),
        ]
        for name, attr in tel_fields:
            tg.addWidget(QLabel(f"{name}:"), r, 0)
            lbl = QLabel("—")
            lbl.setFont(QFont("Consolas", 11))
            lbl.setStyleSheet(
                "background: #f9fafb; border: 1px solid #d1d5db; "
                "border-radius: 3px; padding: 2px 6px;")
            lbl.setFixedWidth(130)
            setattr(self, attr, lbl)
            tg.addWidget(lbl, r, 1)
            r += 1
        tel_grp.setLayout(tg)
        right.addWidget(tel_grp)
        right.addStretch()

        tic_row.addLayout(right, 1)
        outer.addLayout(tic_row)

        # ---- Bottom row: valve controls ----
        valve_row = QHBoxLayout()
        valve_row.setSpacing(10)

        # ---- Solenoid valve ----
        sv_grp = QGroupBox("N₂ Solenoid Valve")
        sg = QVBoxLayout()

        sv_conn_row = QHBoxLayout()
        sv_conn_row.addWidget(QLabel("Port:"))
        self._sv_port_edit = QLineEdit("COM5")
        self._sv_port_edit.setFixedWidth(70)
        sv_conn_row.addWidget(self._sv_port_edit)
        self._sv_connect_btn = QPushButton("Connect")
        self._sv_connect_btn.clicked.connect(self._on_sv_connect)
        sv_conn_row.addWidget(self._sv_connect_btn)
        self._sv_disconnect_btn = QPushButton("Disconnect")
        self._sv_disconnect_btn.setEnabled(False)
        self._sv_disconnect_btn.clicked.connect(self._on_sv_disconnect)
        sv_conn_row.addWidget(self._sv_disconnect_btn)
        sv_conn_row.addStretch()
        sg.addLayout(sv_conn_row)

        sv_status_row = QHBoxLayout()
        self._sv_conn_lbl = QLabel("Disconnected")
        self._sv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        sv_status_row.addWidget(self._sv_conn_lbl)
        sv_status_row.addStretch()
        self._sv_state_lbl = QLabel("State: Unknown")
        self._sv_state_lbl.setFont(QFont("Consolas", 10, QFont.Bold))
        sv_status_row.addWidget(self._sv_state_lbl)
        sg.addLayout(sv_status_row)

        sv_btn_row = QHBoxLayout()
        self._sv_open_btn = QPushButton("Open")
        self._sv_open_btn.setEnabled(False)
        self._sv_open_btn.clicked.connect(self._on_sv_open)
        sv_btn_row.addWidget(self._sv_open_btn)
        self._sv_close_btn = QPushButton("Close")
        self._sv_close_btn.setEnabled(False)
        self._sv_close_btn.clicked.connect(self._on_sv_close)
        sv_btn_row.addWidget(self._sv_close_btn)
        sv_btn_row.addStretch()
        sg.addLayout(sv_btn_row)

        sv_pulse_row = QHBoxLayout()
        sv_pulse_row.addWidget(QLabel("Pulse:"))
        self._sv_pulse_spin = QDoubleSpinBox()
        self._sv_pulse_spin.setRange(0.1, 60.0)
        self._sv_pulse_spin.setValue(1.0)
        self._sv_pulse_spin.setSuffix(" s")
        self._sv_pulse_spin.setDecimals(1)
        self._sv_pulse_spin.setFixedWidth(80)
        sv_pulse_row.addWidget(self._sv_pulse_spin)
        self._sv_pulse_btn = QPushButton("Pulse")
        self._sv_pulse_btn.setEnabled(False)
        self._sv_pulse_btn.clicked.connect(self._on_sv_pulse)
        sv_pulse_row.addWidget(self._sv_pulse_btn)
        sv_pulse_row.addStretch()
        sg.addLayout(sv_pulse_row)

        sg.addStretch()
        sv_grp.setLayout(sg)
        valve_row.addWidget(sv_grp, 1)

        # ---- Butterfly valve ----
        bv_grp = QGroupBox("Butterfly Valve (Foreline)")
        bg = QVBoxLayout()

        bv_conn_row = QHBoxLayout()
        bv_conn_row.addWidget(QLabel("Port:"))
        self._bv_port_edit = QLineEdit("COM14")
        self._bv_port_edit.setFixedWidth(70)
        bv_conn_row.addWidget(self._bv_port_edit)
        self._bv_connect_btn = QPushButton("Connect")
        self._bv_connect_btn.clicked.connect(self._on_bv_connect)
        bv_conn_row.addWidget(self._bv_connect_btn)
        self._bv_disconnect_btn = QPushButton("Disconnect")
        self._bv_disconnect_btn.setEnabled(False)
        self._bv_disconnect_btn.clicked.connect(self._on_bv_disconnect)
        bv_conn_row.addWidget(self._bv_disconnect_btn)
        bv_conn_row.addStretch()
        bg.addLayout(bv_conn_row)

        bv_status_row = QHBoxLayout()
        self._bv_conn_lbl = QLabel("Disconnected")
        self._bv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        bv_status_row.addWidget(self._bv_conn_lbl)
        bv_status_row.addStretch()
        self._bv_pos_lbl = QLabel("Position: —°")
        self._bv_pos_lbl.setFont(QFont("Consolas", 10, QFont.Bold))
        bv_status_row.addWidget(self._bv_pos_lbl)
        bg.addLayout(bv_status_row)

        bv_btn_row = QHBoxLayout()
        self._bv_open_btn = QPushButton("Open (90°)")
        self._bv_open_btn.setEnabled(False)
        self._bv_open_btn.clicked.connect(self._on_bv_open)
        bv_btn_row.addWidget(self._bv_open_btn)
        self._bv_close_btn = QPushButton("Close (0°)")
        self._bv_close_btn.setEnabled(False)
        self._bv_close_btn.clicked.connect(self._on_bv_close)
        bv_btn_row.addWidget(self._bv_close_btn)
        self._bv_stop_btn = QPushButton("Stop")
        self._bv_stop_btn.setEnabled(False)
        self._bv_stop_btn.clicked.connect(self._on_bv_stop)
        bv_btn_row.addWidget(self._bv_stop_btn)
        bv_btn_row.addStretch()
        bg.addLayout(bv_btn_row)

        bv_angle_row = QHBoxLayout()
        bv_angle_row.addWidget(QLabel("Set angle:"))
        self._bv_angle_spin = QDoubleSpinBox()
        self._bv_angle_spin.setRange(0.0, 90.0)
        self._bv_angle_spin.setValue(45.0)
        self._bv_angle_spin.setSuffix("°")
        self._bv_angle_spin.setDecimals(1)
        self._bv_angle_spin.setFixedWidth(80)
        bv_angle_row.addWidget(self._bv_angle_spin)
        self._bv_set_angle_btn = QPushButton("Set")
        self._bv_set_angle_btn.setEnabled(False)
        self._bv_set_angle_btn.clicked.connect(self._on_bv_set_angle)
        bv_angle_row.addWidget(self._bv_set_angle_btn)
        bv_angle_row.addStretch()
        bg.addLayout(bv_angle_row)

        bv_ramp_row = QHBoxLayout()
        bv_ramp_row.addWidget(QLabel("Ramp to:"))
        self._bv_ramp_target_spin = QDoubleSpinBox()
        self._bv_ramp_target_spin.setRange(0.0, 90.0)
        self._bv_ramp_target_spin.setValue(45.0)
        self._bv_ramp_target_spin.setSuffix("°")
        self._bv_ramp_target_spin.setDecimals(1)
        self._bv_ramp_target_spin.setFixedWidth(80)
        bv_ramp_row.addWidget(self._bv_ramp_target_spin)
        bv_ramp_row.addWidget(QLabel("at"))
        self._bv_ramp_rate_spin = QDoubleSpinBox()
        self._bv_ramp_rate_spin.setRange(0.1, 90.0)
        self._bv_ramp_rate_spin.setValue(5.0)
        self._bv_ramp_rate_spin.setSuffix("°/s")
        self._bv_ramp_rate_spin.setDecimals(1)
        self._bv_ramp_rate_spin.setFixedWidth(80)
        bv_ramp_row.addWidget(self._bv_ramp_rate_spin)
        self._bv_start_ramp_btn = QPushButton("Start Ramp")
        self._bv_start_ramp_btn.setEnabled(False)
        self._bv_start_ramp_btn.clicked.connect(self._on_bv_start_ramp)
        bv_ramp_row.addWidget(self._bv_start_ramp_btn)
        self._bv_stop_ramp_btn = QPushButton("Stop Ramp")
        self._bv_stop_ramp_btn.setEnabled(False)
        self._bv_stop_ramp_btn.clicked.connect(self._on_bv_stop_ramp)
        bv_ramp_row.addWidget(self._bv_stop_ramp_btn)
        bv_ramp_row.addStretch()
        bg.addLayout(bv_ramp_row)

        bv_home_row = QHBoxLayout()
        self._bv_home_btn = QPushButton("Home Valve")
        self._bv_home_btn.setEnabled(False)
        self._bv_home_btn.clicked.connect(self._on_bv_home)
        bv_home_row.addWidget(self._bv_home_btn)
        bv_home_row.addStretch()
        bg.addLayout(bv_home_row)

        bg.addStretch()
        bv_grp.setLayout(bg)
        valve_row.addWidget(bv_grp, 2)

        outer.addLayout(valve_row)
        return widget

    # ------------------------------------------------------------------
    # TIC slots
    # ------------------------------------------------------------------

    def _on_tic_connect(self) -> None:
        try:
            from tic_controller import TICController
        except ImportError:
            self._tic_conn_lbl.setText("Library not found")
            self._tic_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status(
                "Edwards TIC: resources/EDWARDS-TIC not found. "
                "Run: git submodule update --init")
            return

        port     = self._tic_port_edit.text().strip() or "COM3"
        baudrate = int(self._tic_baud_edit.text().strip() or "9600")

        self._tic_conn_lbl.setText("Connecting…")
        self._tic_conn_lbl.setStyleSheet("color: orange; font-weight: bold;")

        tic = TICController(port, baudrate=baudrate)
        if tic.connect():
            self._tic_ctrl = tic
            self._tic_conn_lbl.setText(f"Connected — {port}")
            self._tic_conn_lbl.setStyleSheet(
                "color: green; font-weight: bold;")
            self._tic_connect_btn.setEnabled(False)
            self._tic_disconnect_btn.setEnabled(True)
            self._tic_port_edit.setReadOnly(True)
            self._tic_baud_edit.setReadOnly(True)
            self._tic_start_btn.setEnabled(True)
            self._tic_stop_btn.setEnabled(True)
            self._append_status(f"Edwards TIC connected on {port}")
            if self._tic_autopoll_cb.isChecked():
                self._tic_timer.start(self._tic_interval_spin.value())
            self._on_tic_poll()
        else:
            self._tic_conn_lbl.setText("Connection failed")
            self._tic_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status(
                f"Edwards TIC: connection failed on {port}")

    def _on_tic_disconnect(self) -> None:
        self._tic_timer.stop()
        if self._tic_ctrl is not None:
            self._tic_ctrl.disconnect()
            self._tic_ctrl = None
        self._tic_conn_lbl.setText("Disconnected")
        self._tic_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        self._tic_connect_btn.setEnabled(True)
        self._tic_disconnect_btn.setEnabled(False)
        self._tic_port_edit.setReadOnly(False)
        self._tic_baud_edit.setReadOnly(False)
        self._tic_start_btn.setEnabled(False)
        self._tic_stop_btn.setEnabled(False)
        self._append_status("Edwards TIC disconnected")

    def _on_tic_autopoll_toggled(self, checked: bool) -> None:
        if checked and self._tic_ctrl is not None:
            self._tic_timer.start(self._tic_interval_spin.value())
        else:
            self._tic_timer.stop()

    def _on_tic_poll(self) -> None:
        """Kick off a background poll — ignored if one is already running."""
        if self._tic_ctrl is None or self._tic_poll_worker is not None:
            return
        self._tic_poll_worker = _TICPollWorker(self._tic_ctrl)
        self._tic_poll_worker.finished.connect(self._on_tic_poll_done)
        self._tic_poll_worker.error.connect(self._on_tic_poll_error)
        self._tic_poll_worker.finished.connect(
            lambda _: setattr(self, "_tic_poll_worker", None))
        self._tic_poll_worker.error.connect(
            lambda _: setattr(self, "_tic_poll_worker", None))
        self._tic_poll_worker.start()

    def _on_tic_poll_done(self, status: dict) -> None:
        g = status.get("gauges", {})
        p = status.get("pump",   {})
        self._last_pump_state = dict(p)   # cache for valve safety checks
        self._tic_publisher.update(g.get("wrg_mbar"), g.get("apgx_mbar"))

        # Pressure labels with colour coding
        for mbar, lbl in ((g.get("wrg_mbar"),  self._tic_wrg_lbl),
                          (g.get("apgx_mbar"), self._tic_apgx_lbl)):
            if mbar is None or mbar != mbar:   # None or NaN
                lbl.setText("ERROR")
                lbl.setStyleSheet(
                    "background: #fee2e2; border: 1px solid #fca5a5; "
                    "border-radius: 4px; padding: 4px 8px; color: #b91c1c;")
            else:
                lbl.setText(f"{mbar:.3e} mbar")
                if mbar < 1e-3:
                    colour = "#dcfce7; border-color:#86efac; color:#166534;"
                elif mbar < 1.0:
                    colour = "#fef9c3; border-color:#fde047; color:#713f12;"
                else:
                    colour = "#f9fafb; border-color:#d1d5db; color:#111827;"
                lbl.setStyleSheet(
                    f"background:{colour} "
                    "border: 1px solid; border-radius: 4px; padding: 4px 8px;")

        # Pump telemetry
        def _fmt_opt(v, fmt, unit=""):
            return f"{v:{fmt}}{unit}" if v is not None and v == v else "—"

        self._tic_tel_status.setText(p.get("status_str", "—"))
        self._tic_tel_speed.setText(
            _fmt_opt(p.get("speed_pct"),  ".0f", " %"))
        self._tic_tel_power.setText(
            _fmt_opt(p.get("power_w"),    ".1f", " W"))
        self._tic_tel_current.setText(
            _fmt_opt(p.get("current_a"),  ".2f", " A"))
        self._tic_tel_voltage.setText(
            _fmt_opt(p.get("voltage_v"),  ".1f", " V"))
        self._tic_tel_temp.setText(
            _fmt_opt(p.get("temp_c"),     ".1f", " °C"))

        # Colour-code status label
        s = p.get("status_str", "")
        if "Fault" in s or "FAULT" in s:
            css = "background:#fee2e2; border-color:#fca5a5; color:#b91c1c;"
        elif "At Speed" in s:
            css = "background:#dcfce7; border-color:#86efac; color:#166534;"
        elif "Accel" in s or "Running" in s:
            css = "background:#fef9c3; border-color:#fde047; color:#713f12;"
        else:
            css = "background:#f9fafb; border-color:#d1d5db; color:#111827;"
        self._tic_tel_status.setStyleSheet(
            f"{css} border: 1px solid; border-radius: 3px; padding: 2px 6px;")

    def _on_tic_poll_error(self, msg: str) -> None:
        self._append_status(f"Edwards TIC poll error: {msg}")

    def _on_tic_start_pump(self) -> None:
        if self._tic_ctrl is None:
            return
        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok = self._c.start_pump()
                    self.done.emit(ok, "Pump started" if ok else "Start failed")
                except Exception as exc:
                    self.done.emit(False, f"start_pump error: {exc}")
        w = _W(self._tic_ctrl)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        w.done.connect(lambda *_: self._on_tic_poll())
        self._tic_cmd_workers.append(w)
        w.finished.connect(lambda: self._tic_cmd_workers.remove(w) if w in self._tic_cmd_workers else None)
        w.start()

    def _on_tic_stop_pump(self) -> None:
        if self._tic_ctrl is None:
            return
        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok = self._c.stop_pump()
                    self.done.emit(ok, "Pump stopped" if ok else "Stop failed")
                except Exception as exc:
                    self.done.emit(False, f"stop_pump error: {exc}")
        w = _W(self._tic_ctrl)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        w.done.connect(lambda *_: self._on_tic_poll())
        self._tic_cmd_workers.append(w)
        w.finished.connect(lambda: self._tic_cmd_workers.remove(w) if w in self._tic_cmd_workers else None)
        w.start()

    def _on_tic_set_speed(self) -> None:
        if self._tic_ctrl is None:
            return
        try:
            pct = int(self._tic_speed_edit.text().strip() or "0")
        except ValueError:
            self._append_status("TIC: invalid speed value")
            return
        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl, p): super().__init__(); self._c = ctrl; self._p = p
            def run(self):
                try:
                    ok = self._c.set_pump_speed(self._p)
                    self.done.emit(ok, f"Speed setpoint → {self._p}%" if ok else "Set speed failed")
                except Exception as exc:
                    self.done.emit(False, f"set_pump_speed error: {exc}")
        w = _W(self._tic_ctrl, pct)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        self._tic_cmd_workers.append(w)
        w.finished.connect(lambda: self._tic_cmd_workers.remove(w) if w in self._tic_cmd_workers else None)
        w.start()

    # ==================================================================
    # Solenoid valve — connection
    # ==================================================================

    def _on_sv_connect(self) -> None:
        try:
            from valve_controller import ValveController
        except ImportError:
            self._sv_conn_lbl.setText("Library not found")
            self._sv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status("Solenoid valve: valve_controller not importable — check submodule")
            return
        port = self._sv_port_edit.text().strip() or None
        try:
            self._sv_ctrl = ValveController(port)
            label = port or "auto"
            self._sv_conn_lbl.setText(f"Connected ({label})")
            self._sv_conn_lbl.setStyleSheet("color: green; font-weight: bold;")
            self._sv_connect_btn.setEnabled(False)
            self._sv_disconnect_btn.setEnabled(True)
            self._sv_open_btn.setEnabled(True)
            self._sv_close_btn.setEnabled(True)
            self._sv_pulse_btn.setEnabled(True)
            self._sv_state_lbl.setText("State: Closed")
            self._append_status(f"Solenoid valve connected ({label})")
        except Exception as exc:
            self._sv_ctrl = None
            self._sv_conn_lbl.setText("Connect failed")
            self._sv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status(f"Solenoid valve connect error: {exc}")

    def _on_sv_disconnect(self) -> None:
        if self._sv_ctrl is not None:
            try:
                self._sv_ctrl.disconnect()
            except Exception:
                pass
            self._sv_ctrl = None
        self._sv_conn_lbl.setText("Disconnected")
        self._sv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        self._sv_connect_btn.setEnabled(True)
        self._sv_disconnect_btn.setEnabled(False)
        self._sv_open_btn.setEnabled(False)
        self._sv_close_btn.setEnabled(False)
        self._sv_pulse_btn.setEnabled(False)
        self._sv_state_lbl.setText("State: Unknown")
        self._append_status("Solenoid valve disconnected")

    # ==================================================================
    # Solenoid valve — safety + actions
    # ==================================================================

    def _solenoid_safety_check_open(self) -> bool:
        """Return True if opening the solenoid is safe (blocking) or user confirms (warning)."""
        p = self._last_pump_state
        running   = p.get("running",   False)
        speed_pct = p.get("speed_pct")

        # Hard block: pump is running or accelerating
        if running:
            QMessageBox.critical(self, "Solenoid Blocked",
                "Cannot open solenoid: the turbo pump is running.\n\n"
                "Stop the pump and wait for it to fully spin down before "
                "opening the N₂ inlet.")
            return False

        # Hard block: spinning down above 50 %
        if speed_pct is not None and speed_pct > 50:
            QMessageBox.critical(self, "Solenoid Blocked",
                f"Cannot open solenoid: turbo is still spinning at {speed_pct:.0f} %.\n\n"
                "Wait until the speed drops below 50 % before opening the N₂ inlet.")
            return False

        # Soft warn: spinning down ≤ 50 %
        if speed_pct is not None and speed_pct > 0:
            ret = QMessageBox.question(self, "Solenoid Warning",
                f"Turbo is spinning down at {speed_pct:.0f} %.\n"
                "Opening the N₂ inlet now may stress the pump bearings.\n\n"
                "Proceed anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return False

        # Soft warn: butterfly valve not fully closed
        if self._bv_position is not None and self._bv_position > 5.0:
            ret = QMessageBox.question(self, "Solenoid Warning",
                f"Butterfly valve is at {self._bv_position:.1f}° (not fully closed).\n"
                "Opening the solenoid with the butterfly open will vent N₂ into the system.\n\n"
                "Proceed anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return False

        return True

    def _on_sv_open(self) -> None:
        if self._sv_ctrl is None:
            return
        if not self._solenoid_safety_check_open():
            return

        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    self._c.open()
                    self.done.emit(True, "Solenoid opened")
                except Exception as exc:
                    self.done.emit(False, f"Solenoid open error: {exc}")

        w = _W(self._sv_ctrl)
        w.done.connect(lambda ok, msg: (
            self._append_status(f"Solenoid: {msg}"),
            self._sv_state_lbl.setText("State: Open" if ok else "State: Error"),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_sv_close(self) -> None:
        if self._sv_ctrl is None:
            return

        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    self._c.close()
                    self.done.emit(True, "Solenoid closed")
                except Exception as exc:
                    self.done.emit(False, f"Solenoid close error: {exc}")

        w = _W(self._sv_ctrl)
        w.done.connect(lambda ok, msg: (
            self._append_status(f"Solenoid: {msg}"),
            self._sv_state_lbl.setText("State: Closed" if ok else "State: Error"),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_sv_pulse(self) -> None:
        if self._sv_ctrl is None:
            return
        if not self._solenoid_safety_check_open():
            return
        duration = self._sv_pulse_spin.value()

        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl, dur): super().__init__(); self._c = ctrl; self._d = dur
            def run(self):
                try:
                    self._c.pulse(self._d)
                    self.done.emit(True, f"Solenoid pulsed {self._d:.1f} s")
                except Exception as exc:
                    self.done.emit(False, f"Solenoid pulse error: {exc}")

        w = _W(self._sv_ctrl, duration)
        w.done.connect(lambda ok, msg: (
            self._append_status(f"Solenoid: {msg}"),
            self._sv_state_lbl.setText("State: Closed" if ok else "State: Error"),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    # ==================================================================
    # Butterfly valve — connection
    # ==================================================================

    def _on_bv_connect(self) -> None:
        try:
            from cv_controller import CommandValveController
        except ImportError:
            self._bv_conn_lbl.setText("Library not found")
            self._bv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status("Butterfly valve: cv_controller not importable — check submodule")
            return
        port = self._bv_port_edit.text().strip() or "COM14"
        try:
            cv = CommandValveController(port)
            if not cv.connect():
                raise RuntimeError("connect() returned False")
            self._bv_ctrl = cv
            self._bv_conn_lbl.setText(f"Connected ({port})")
            self._bv_conn_lbl.setStyleSheet("color: green; font-weight: bold;")
            self._bv_connect_btn.setEnabled(False)
            self._bv_disconnect_btn.setEnabled(True)
            for btn in (self._bv_open_btn, self._bv_close_btn, self._bv_stop_btn,
                        self._bv_set_angle_btn, self._bv_start_ramp_btn, self._bv_home_btn):
                btn.setEnabled(True)
            pos = cv.get_position()
            if pos is not None:
                self._bv_position = pos
                self._bv_pos_lbl.setText(f"Position: {pos:.1f}°")
            self._append_status(f"Butterfly valve connected ({port})")
        except Exception as exc:
            self._bv_ctrl = None
            self._bv_conn_lbl.setText("Connect failed")
            self._bv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
            self._append_status(f"Butterfly valve connect error: {exc}")

    def _on_bv_disconnect(self) -> None:
        if self._bv_ramp_stop is not None:
            self._bv_ramp_stop.set()
            self._bv_ramp_stop = None
        if self._bv_ctrl is not None:
            try:
                self._bv_ctrl.disconnect()
            except Exception:
                pass
            self._bv_ctrl = None
        self._bv_position = None
        self._bv_conn_lbl.setText("Disconnected")
        self._bv_conn_lbl.setStyleSheet("color: red; font-weight: bold;")
        self._bv_connect_btn.setEnabled(True)
        self._bv_disconnect_btn.setEnabled(False)
        for btn in (self._bv_open_btn, self._bv_close_btn, self._bv_stop_btn,
                    self._bv_set_angle_btn, self._bv_start_ramp_btn,
                    self._bv_stop_ramp_btn, self._bv_home_btn):
            btn.setEnabled(False)
        self._bv_pos_lbl.setText("Position: —°")
        self._append_status("Butterfly valve disconnected")

    # ==================================================================
    # Butterfly valve — safety + actions
    # ==================================================================

    def _butterfly_safety_check_close(self) -> bool:
        """Warn and ask user to confirm if the turbo is running before closing butterfly."""
        if self._last_pump_state.get("running", False):
            ret = QMessageBox.question(self, "Butterfly Valve Warning",
                "The turbo pump is running.\n"
                "Closing the butterfly valve will throttle the foreline and stress the pump.\n\n"
                "Proceed anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            return ret == QMessageBox.Yes
        return True

    def _bv_update_position(self, pos: float) -> None:
        self._bv_position = pos
        self._bv_pos_lbl.setText(f"Position: {pos:.1f}°")

    def _on_bv_open(self) -> None:
        if self._bv_ctrl is None:
            return

        class _W(QThread):
            done = pyqtSignal(bool, str, float)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok  = self._c.open()
                    pos = self._c.get_position() or 90.0
                    self.done.emit(ok, "Butterfly opened" if ok else "Open failed", pos)
                except Exception as exc:
                    self.done.emit(False, f"BV open error: {exc}", 0.0)

        w = _W(self._bv_ctrl)
        w.done.connect(lambda ok, msg, pos: (
            self._append_status(f"Butterfly: {msg}"),
            self._bv_update_position(pos),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_bv_close(self) -> None:
        if self._bv_ctrl is None:
            return
        if not self._butterfly_safety_check_close():
            return

        class _W(QThread):
            done = pyqtSignal(bool, str, float)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok  = self._c.close()
                    pos = self._c.get_position() or 0.0
                    self.done.emit(ok, "Butterfly closed" if ok else "Close failed", pos)
                except Exception as exc:
                    self.done.emit(False, f"BV close error: {exc}", 0.0)

        w = _W(self._bv_ctrl)
        w.done.connect(lambda ok, msg, pos: (
            self._append_status(f"Butterfly: {msg}"),
            self._bv_update_position(pos),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_bv_stop(self) -> None:
        if self._bv_ctrl is None:
            return

        class _W(QThread):
            done = pyqtSignal(bool, str, float)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok  = self._c.stop()
                    pos = self._c.get_position() or 0.0
                    self.done.emit(ok, "Butterfly stopped" if ok else "Stop failed", pos)
                except Exception as exc:
                    self.done.emit(False, f"BV stop error: {exc}", 0.0)

        w = _W(self._bv_ctrl)
        w.done.connect(lambda ok, msg, pos: (
            self._append_status(f"Butterfly: {msg}"),
            self._bv_update_position(pos),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_bv_set_angle(self) -> None:
        if self._bv_ctrl is None:
            return
        angle = self._bv_angle_spin.value()

        class _W(QThread):
            done = pyqtSignal(bool, str, float)
            def __init__(self, ctrl, a): super().__init__(); self._c = ctrl; self._a = a
            def run(self):
                try:
                    ok  = self._c.set_angle(self._a)
                    pos = self._c.get_position() or self._a
                    self.done.emit(ok, f"Butterfly → {self._a:.1f}°" if ok else "Set angle failed", pos)
                except Exception as exc:
                    self.done.emit(False, f"BV set angle error: {exc}", 0.0)

        w = _W(self._bv_ctrl, angle)
        w.done.connect(lambda ok, msg, pos: (
            self._append_status(f"Butterfly: {msg}"),
            self._bv_update_position(pos),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_bv_start_ramp(self) -> None:
        if self._bv_ctrl is None:
            return
        if self._bv_ramp_stop is not None:
            self._append_status("Butterfly: ramp already in progress — stop it first")
            return
        target   = self._bv_ramp_target_spin.value()
        rate     = self._bv_ramp_rate_spin.value()
        stop_evt = threading.Event()
        self._bv_ramp_stop = stop_evt

        class _W(QThread):
            done   = pyqtSignal(bool, str)
            update = pyqtSignal(float)
            def __init__(self, ctrl, t, r, ev):
                super().__init__()
                self._c, self._t, self._r, self._ev = ctrl, t, r, ev
            def run(self):
                ok = self._c.ramp_to_angle(
                    self._t, self._r, self._ev,
                    on_update=lambda a: self.update.emit(a))
                self.done.emit(ok,
                    f"Ramp complete — reached {self._t:.1f}°" if ok else "Ramp cancelled")

        w = _W(self._bv_ctrl, target, rate, stop_evt)
        w.update.connect(self._bv_update_position)
        w.done.connect(lambda ok, msg: (
            self._append_status(f"Butterfly: {msg}"),
            setattr(self, "_bv_ramp_stop", None),
            self._bv_stop_ramp_btn.setEnabled(False),
        ))
        self._bv_stop_ramp_btn.setEnabled(True)
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    def _on_bv_stop_ramp(self) -> None:
        if self._bv_ramp_stop is not None:
            self._bv_ramp_stop.set()

    def _on_bv_home(self) -> None:
        if self._bv_ctrl is None:
            return

        class _W(QThread):
            done = pyqtSignal(bool, str, float)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self):
                try:
                    ok  = self._c.home()
                    pos = self._c.get_position() or 0.0
                    self.done.emit(ok,
                        "Butterfly homed" if ok else "Homing failed — check error code", pos)
                except Exception as exc:
                    self.done.emit(False, f"BV home error: {exc}", 0.0)

        w = _W(self._bv_ctrl)
        w.done.connect(lambda ok, msg, pos: (
            self._append_status(f"Butterfly: {msg}"),
            self._bv_update_position(pos),
        ))
        self._valve_cmd_workers.append(w)
        w.finished.connect(lambda: self._valve_cmd_workers.remove(w)
                           if w in self._valve_cmd_workers else None)
        w.start()

    # ==================================================================
    # Cleanup
    # ==================================================================

    def closeEvent(self, event) -> None:
        self._autosave_timer.stop()
        self._autosave_state()   # final save before teardown
        self._tic_timer.stop()
        if self._tic_ctrl is not None:
            self._tic_ctrl.disconnect()
        if self._bv_ramp_stop is not None:
            self._bv_ramp_stop.set()
        if self._bv_ctrl is not None:
            try:
                self._bv_ctrl.disconnect()
            except Exception:
                pass
        if self._sv_ctrl is not None:
            try:
                self._sv_ctrl.disconnect()
            except Exception:
                pass
        self._resources.teardown_all()
        self._ctrl.disconnect()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class FPGAWindow(QMainWindow):
    """Standalone window wrapper for FPGAWidget."""

    def __init__(self, controller: FPGAController | None = None):
        super().__init__()
        self.setWindowTitle("usphere — FPGA Control")
        self.resize(1400, 900)
        icon_path = Path(__file__).parent / "assets" / "Logo_transparent_outlined.PNG"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self._widget = FPGAWidget(controller=controller)
        self.setCentralWidget(self._widget)

    def closeEvent(self, event):
        self._widget.closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = FPGAWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
