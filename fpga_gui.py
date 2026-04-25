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
from pathlib import Path

import numpy as np

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
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

from fpga_core import FPGAConfig, FPGAController, load_last_session, _append_log
from modules import discover_hardware_modules
from procedures.base import LiveFPGAFacade
from fpga_registers import (
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
from fpga_plot import ALL_PLOT_NAMES, FPGAPlotWidget
from fpga_ipc import TICPublisher
from arb_waveform import (
    generate_comb,
    generate_sine,
    generate_triangle,
    generate_trapezoid,
    save_waveform,
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
# Procedures tab
# ---------------------------------------------------------------------------

class _ProcedureManagerWidget(QWidget):
    """
    Manages loading and unloading of control procedures (procedures/proc_*.py).

    Each available procedure is listed with a Load / Unload button.  Loading
    instantiates the procedure, injects a LiveFPGAFacade, calls create_widget(),
    and adds the resulting widget as a new top-level tab.  Unloading removes
    the tab and calls teardown().

    Call notify_fpga_update(state) each monitor cycle to forward the FPGA
    register snapshot to all loaded procedures' on_fpga_update() hooks.
    """

    def __init__(self, tabs: QTabWidget, fpga_controller: FPGAController,
                 parent=None):
        super().__init__(parent)
        self._tabs = tabs
        self._ctrl = fpga_controller
        self._loaded: dict[str, tuple[object, QWidget]] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._available: list = []

        try:
            from procedures import discover_procedures
            self._available = discover_procedures()
        except Exception as exc:
            print(f"[procedures] Discovery failed: {exc}")

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QLabel(
            "<b>Control Procedures</b><br>"
            "<span style='color:gray; font-size:10px;'>"
            "Load a procedure to add its control tab to the GUI.  "
            "The FPGA must be connected before running any procedure.</span>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        if not self._available:
            layout.addWidget(QLabel(
                "No procedures found in procedures/  "
                "(files must be named proc_*.py and expose a Procedure class)."))
            layout.addStretch()
            return

        for cls in self._available:
            grp = QGroupBox(cls.NAME)
            gl = QVBoxLayout(grp)

            if cls.DESCRIPTION:
                desc = QLabel(cls.DESCRIPTION)
                desc.setWordWrap(True)
                desc.setStyleSheet("color: gray; font-size: 10px;")
                gl.addWidget(desc)

            btn_row = QHBoxLayout()
            btn = QPushButton("Load")
            btn.setFixedWidth(90)
            btn.setStyleSheet(
                "QPushButton { background-color: #2563eb; color: white; "
                "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
                "QPushButton:hover { background-color: #3b82f6; }"
            )
            btn.clicked.connect(lambda _, c=cls: self._toggle(c))
            self._buttons[cls.NAME] = btn
            btn_row.addWidget(btn)
            btn_row.addStretch()
            gl.addLayout(btn_row)
            layout.addWidget(grp)

        layout.addStretch()

    def _toggle(self, cls) -> None:
        if cls.NAME in self._loaded:
            self._unload(cls.NAME)
        else:
            self._load(cls)

    def _load(self, cls) -> None:
        proc = cls()
        proc.fpga = LiveFPGAFacade(self._ctrl)
        try:
            widget = proc.create_widget()
        except Exception as exc:
            print(f"[procedures] {cls.NAME} create_widget failed: {exc}")
            return
        self._tabs.addTab(widget, proc.NAME)
        self._loaded[proc.NAME] = (proc, widget)
        self._tabs.setCurrentWidget(widget)
        btn = self._buttons.get(proc.NAME)
        if btn:
            btn.setText("Unload")
            btn.setStyleSheet(
                "QPushButton { background-color: #c0392b; color: white; "
                "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
                "QPushButton:hover { background-color: #e74c3c; }"
            )

    def _unload(self, name: str) -> None:
        proc, widget = self._loaded.pop(name)
        idx = self._tabs.indexOf(widget)
        if idx >= 0:
            self._tabs.removeTab(idx)
        try:
            proc.teardown()
        except Exception as exc:
            print(f"[procedures] {name} teardown error: {exc}")
        widget.deleteLater()
        btn = self._buttons.get(name)
        if btn:
            btn.setText("Load")
            btn.setStyleSheet(
                "QPushButton { background-color: #2563eb; color: white; "
                "font-weight: bold; padding: 3px 8px; border-radius: 3px; }"
                "QPushButton:hover { background-color: #3b82f6; }"
            )

    def notify_fpga_update(self, state: dict) -> None:
        """Forward the FPGA register snapshot to all loaded procedures."""
        for name, (proc, _widget) in self._loaded.items():
            try:
                proc.on_fpga_update(state)
            except Exception as exc:
                print(f"[procedures] {name} on_fpga_update error: {exc}")

    def teardown_all(self) -> None:
        """Teardown all loaded procedures (call on app close)."""
        for name in list(self._loaded.keys()):
            self._unload(name)


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
        self._tic_timer       = QTimer(self)
        self._tic_timer.timeout.connect(self._on_tic_poll)

        # Waveform designer state
        self._wd_result = None          # last generated WaveformResult
        self._wd_mc_worker: _CombMCWorker | None = None
        self._wd_mc_stop_flag: list[bool] = [False]
        self._wd_has_plot: bool = False  # set in _build_waveform_designer

        self._build_ui()
        self._restore_session()

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

        # Procedure manager (Load / Unload buttons; loaded procedures add more tabs)
        self._proc_manager = _ProcedureManagerWidget(tabs, self._ctrl)
        tabs.addTab(self._proc_manager, "Procedures")

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
        ig_name = f"ig {a}" if a == "X" else f" ig {a}"
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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        # Waveform designer (generates waveforms for the arb buffer)
        layout.addWidget(self._build_waveform_designer())

        # Buffer file
        file_grp = QGroupBox("Bead Arbitrary Drive")
        fl = QVBoxLayout()

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Buffer file path:"))
        self._arb_file_edit = QLineEdit()
        path_row.addWidget(self._arb_file_edit, 1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_arb_file)
        path_row.addWidget(browse_btn)
        fl.addLayout(path_row)

        write_btn = QPushButton("Write Data to Buffers")
        write_btn.clicked.connect(self._on_write_arb_buffers)
        fl.addWidget(write_btn)

        file_grp.setLayout(fl)
        layout.addWidget(file_grp)

        # Arb gains
        gain_grp = QGroupBox("Arb Gain")
        gg = QGridLayout()
        r = 0
        r = self._add_reg(gg, r, "Arb gain (ch0)")
        r = self._add_reg(gg, r, "Arb gain (ch1)")
        r = self._add_reg(gg, r, "Arb gain (ch2)")
        r = self._add_reg(gg, r, "Arb steps per cycle")
        r = self._add_reg(gg, r, "ready_to_write")
        r = self._add_reg(gg, r, "written_address")
        gain_grp.setLayout(gg)
        layout.addWidget(gain_grp)

        # Ramp arb drive
        ramp_grp = QGroupBox("Ramp Arb Drive")
        rg = QGridLayout()
        r = 0
        r = self._add_host(rg, r, "End value arb (ch0)")
        r = self._add_host(rg, r, "Step arb (ch0)")
        r = self._add_host(rg, r, "End value arb (ch1)")
        r = self._add_host(rg, r, "Step arb (ch1)")
        r = self._add_host(rg, r, "Delay Time (s) arb")
        r = self._add_host(rg, r, "z arb scale")
        ramp_btn = QPushButton("Ramp Arb Drive")
        ramp_btn.clicked.connect(self._on_ramp_arb)
        rg.addWidget(ramp_btn, r, 0, 1, 3)
        ramp_grp.setLayout(rg)
        layout.addWidget(ramp_grp)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)
        return widget

    # ------------------------------------------------------------------
    # Waveform designer group
    # ------------------------------------------------------------------

    def _build_waveform_designer(self) -> QGroupBox:
        """Build the waveform designer: type selector, params, MC, preview."""
        grp = QGroupBox("Waveform Designer")
        layout = QVBoxLayout(grp)
        layout.setSpacing(6)

        # ---- Common params ----
        common = QHBoxLayout()
        common.addWidget(QLabel("Points:"))
        self._wd_npoints_combo = QComboBox()
        self._wd_npoints_combo.addItems(
            ["1000", "2000", "5000", "10000", "20000", "50000", "100000"])
        self._wd_npoints_combo.setCurrentIndex(3)   # 10 000
        common.addWidget(self._wd_npoints_combo)
        common.addSpacing(12)
        common.addWidget(QLabel("Sample rate (Sa/s):"))
        self._wd_samplerate_edit = QLineEdit("1000000")
        self._wd_samplerate_edit.setFixedWidth(100)
        common.addWidget(self._wd_samplerate_edit)
        common.addStretch()
        layout.addLayout(common)

        # ---- Per-type parameter tabs ----
        self._wd_type_tabs = QTabWidget()
        self._wd_type_tabs.setTabPosition(QTabWidget.North)

        # -- Sine --
        sine_w = QWidget()
        sg = QGridLayout(sine_w)
        sg.addWidget(QLabel("Cycles:"), 0, 0)
        self._wd_sine_ncycles = QLineEdit("1.0")
        self._wd_sine_ncycles.setFixedWidth(80)
        sg.addWidget(self._wd_sine_ncycles, 0, 1)
        sg.addWidget(QLabel("Phase (°):"), 1, 0)
        self._wd_sine_phase = QLineEdit("0.0")
        self._wd_sine_phase.setFixedWidth(80)
        sg.addWidget(self._wd_sine_phase, 1, 1)
        sg.setColumnStretch(2, 1)
        self._wd_type_tabs.addTab(sine_w, "Sine")

        # -- Triangle --
        tri_w = QWidget()
        tg = QGridLayout(tri_w)
        tg.addWidget(QLabel("Cycles:"), 0, 0)
        self._wd_tri_ncycles = QLineEdit("1.0")
        self._wd_tri_ncycles.setFixedWidth(80)
        tg.addWidget(self._wd_tri_ncycles, 0, 1)
        tg.addWidget(QLabel("Symmetry (0–1):"), 1, 0)
        tg.addWidget(QLabel(
            "  0=falling saw  0.5=triangle  1=rising saw"),
            1, 2, 1, 2)
        self._wd_tri_symmetry = QLineEdit("0.5")
        self._wd_tri_symmetry.setFixedWidth(80)
        tg.addWidget(self._wd_tri_symmetry, 1, 1)
        tg.setColumnStretch(4, 1)
        self._wd_type_tabs.addTab(tri_w, "Triangle")

        # -- Trapezoid --
        trap_w = QWidget()
        trg = QGridLayout(trap_w)
        trg.addWidget(QLabel("Cycles:"), 0, 0)
        self._wd_trap_ncycles = QLineEdit("1.0")
        self._wd_trap_ncycles.setFixedWidth(80)
        trg.addWidget(self._wd_trap_ncycles, 0, 1)
        trg.addWidget(QLabel("Rise frac:"), 1, 0)
        self._wd_trap_rise = QLineEdit("0.1")
        self._wd_trap_rise.setFixedWidth(80)
        trg.addWidget(self._wd_trap_rise, 1, 1)
        trg.addWidget(QLabel("High frac:"), 2, 0)
        self._wd_trap_high = QLineEdit("0.4")
        self._wd_trap_high.setFixedWidth(80)
        trg.addWidget(self._wd_trap_high, 2, 1)
        trg.addWidget(QLabel("Fall frac:"), 3, 0)
        self._wd_trap_fall = QLineEdit("0.1")
        self._wd_trap_fall.setFixedWidth(80)
        trg.addWidget(self._wd_trap_fall, 3, 1)
        self._wd_trap_low_lbl = QLabel("Low frac: 0.400")
        self._wd_trap_low_lbl.setStyleSheet("color: gray; font-size: 10px;")
        trg.addWidget(self._wd_trap_low_lbl, 4, 0, 1, 3)
        for e in (self._wd_trap_rise, self._wd_trap_high, self._wd_trap_fall):
            e.textChanged.connect(self._on_wd_trap_frac_changed)
        trg.setColumnStretch(2, 1)
        self._wd_type_tabs.addTab(trap_w, "Trapezoid")

        # -- Freq Comb --
        comb_w = QWidget()
        cl = QVBoxLayout(comb_w)
        cl.setSpacing(4)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Frequency mode:"))
        self._wd_comb_mode = QComboBox()
        self._wd_comb_mode.addItems(
            ["List (comma-sep Hz)", "Range (arange)", "Trap fractions"])
        mode_row.addWidget(self._wd_comb_mode)
        mode_row.addStretch()
        cl.addLayout(mode_row)

        self._wd_comb_stack = QStackedWidget()

        # Page 0: list
        list_w = QWidget()
        ll = QHBoxLayout(list_w)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Frequencies (Hz):"))
        self._wd_comb_list_edit = QLineEdit("100000, 200000, 300000, 400000, 500000, 600000, 700000")
        ll.addWidget(self._wd_comb_list_edit, 1)
        self._wd_comb_stack.addWidget(list_w)

        # Page 1: arange
        arange_w = QWidget()
        ag = QGridLayout(arange_w)
        ag.setContentsMargins(0, 0, 0, 0)
        ag.addWidget(QLabel("Start (Hz):"), 0, 0)
        self._wd_comb_arange_start = QLineEdit("100000")
        self._wd_comb_arange_start.setFixedWidth(90)
        ag.addWidget(self._wd_comb_arange_start, 0, 1)
        ag.addWidget(QLabel("Stop (Hz):"), 0, 2)
        self._wd_comb_arange_stop = QLineEdit("700000")
        self._wd_comb_arange_stop.setFixedWidth(90)
        ag.addWidget(self._wd_comb_arange_stop, 0, 3)
        ag.addWidget(QLabel("Step (Hz):"), 0, 4)
        self._wd_comb_arange_step = QLineEdit("100000")
        self._wd_comb_arange_step.setFixedWidth(90)
        ag.addWidget(self._wd_comb_arange_step, 0, 5)
        ag.setColumnStretch(6, 1)
        self._wd_comb_stack.addWidget(arange_w)

        # Page 2: trap fractions
        frac_w = QWidget()
        fg = QGridLayout(frac_w)
        fg.setContentsMargins(0, 0, 0, 0)
        fg.addWidget(QLabel("Trap freq (Hz):"), 0, 0)
        self._wd_comb_trap_freq = QLineEdit("150000")
        self._wd_comb_trap_freq.setFixedWidth(90)
        fg.addWidget(self._wd_comb_trap_freq, 0, 1)
        fg.addWidget(QLabel("Fractions:"), 0, 2)
        self._wd_comb_fracs_edit = QLineEdit("1, 2, 3, 4, 5")
        fg.addWidget(self._wd_comb_fracs_edit, 0, 3)
        fg.setColumnStretch(4, 1)
        self._wd_comb_stack.addWidget(frac_w)

        self._wd_comb_mode.currentIndexChanged.connect(
            self._on_wd_comb_mode_changed)
        cl.addWidget(self._wd_comb_stack)

        mc_row = QHBoxLayout()
        mc_row.addWidget(QLabel("MC trials:"))
        self._wd_comb_trials = QLineEdit("1000")
        self._wd_comb_trials.setFixedWidth(70)
        mc_row.addWidget(self._wd_comb_trials)
        mc_row.addStretch()
        cl.addLayout(mc_row)

        self._wd_mc_progress = QProgressBar()
        self._wd_mc_progress.setRange(0, 100)
        self._wd_mc_progress.setFixedHeight(14)
        self._wd_mc_progress.hide()
        cl.addWidget(self._wd_mc_progress)

        self._wd_type_tabs.addTab(comb_w, "Freq Comb")
        layout.addWidget(self._wd_type_tabs)

        # ---- Generate / Cancel row ----
        gen_row = QHBoxLayout()
        self._wd_gen_btn = QPushButton("Generate")
        self._wd_gen_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 14px; "
            "background-color: #2563eb; color: white; border-radius: 3px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
            "QPushButton:disabled { background-color: #93c5fd; }")
        self._wd_gen_btn.clicked.connect(self._on_wd_generate)
        gen_row.addWidget(self._wd_gen_btn)
        self._wd_cancel_btn = QPushButton("Cancel")
        self._wd_cancel_btn.setStyleSheet(
            "QPushButton { color: #b91c1c; padding: 5px 10px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #fee2e2; }")
        self._wd_cancel_btn.clicked.connect(self._on_wd_cancel)
        self._wd_cancel_btn.hide()
        gen_row.addWidget(self._wd_cancel_btn)
        gen_row.addStretch()
        layout.addLayout(gen_row)

        # ---- Stats row ----
        stats_row = QHBoxLayout()
        stats_row.addWidget(QLabel("RMS/peak:"))
        self._wd_rms_lbl = QLabel("—")
        self._wd_rms_lbl.setFixedWidth(60)
        stats_row.addWidget(self._wd_rms_lbl)
        stats_row.addWidget(QLabel("Crest factor:"))
        self._wd_crest_lbl = QLabel("—")
        self._wd_crest_lbl.setFixedWidth(55)
        stats_row.addWidget(self._wd_crest_lbl)
        stats_row.addSpacing(8)
        self._wd_desc_lbl = QLabel("")
        self._wd_desc_lbl.setStyleSheet(
            "color: #6b7280; font-size: 10px; font-style: italic;")
        stats_row.addWidget(self._wd_desc_lbl, 1)
        layout.addLayout(stats_row)

        # ---- Preview plot ----
        try:
            import pyqtgraph as pg
            self._wd_plot_widget = pg.PlotWidget(background="w")
            self._wd_plot_widget.setFixedHeight(160)
            pi = self._wd_plot_widget.getPlotItem()
            pi.setLabel("left", "Amplitude")
            pi.setLabel("bottom", "Sample index")
            pi.showGrid(x=True, y=True, alpha=0.25)
            self._wd_plot_curve = pi.plot(
                pen=pg.mkPen("#2563eb", width=1.5))
            layout.addWidget(self._wd_plot_widget)
            self._wd_has_plot = True
        except ImportError:
            layout.addWidget(
                QLabel("(install pyqtgraph for waveform preview)"))
            self._wd_has_plot = False

        # ---- Save / Write row ----
        save_row = QHBoxLayout()
        self._wd_save_btn = QPushButton("Save to File…")
        self._wd_save_btn.clicked.connect(self._on_wd_save)
        save_row.addWidget(self._wd_save_btn)
        self._wd_write_btn = QPushButton("Write to FPGA")
        self._wd_write_btn.setStyleSheet(
            "QPushButton { padding: 5px 10px; background-color: #059669; "
            "color: white; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #10b981; }"
            "QPushButton:disabled { background-color: #6ee7b7; }")
        self._wd_write_btn.clicked.connect(self._on_wd_write_fpga)
        save_row.addWidget(self._wd_write_btn)
        save_row.addStretch()
        layout.addLayout(save_row)

        return grp

    # ------------------------------------------------------------------
    # Outputs tab (EOM + COM + AO rotation)
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

    def _restore_session(self) -> None:
        last = load_last_session()
        if last is None:
            return
        try:
            cfg_data = last.get("config") or last
            if "bitfile" in cfg_data:
                self._bitfile_edit.setText(cfg_data["bitfile"])
            if "resource" in cfg_data:
                self._resource_edit.setText(cfg_data["resource"])
            if "poll_interval_ms" in cfg_data:
                self._poll_spin.setValue(int(cfg_data["poll_interval_ms"]))
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
        self._update_reg_edits(values)
        self._proc_manager.notify_fpga_update(values)

    def _on_plot_data(self, values: dict) -> None:
        self._plot_widget.push_values(values)

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
        # integral gain has leading space for Y/Z
        if a == "X":
            pid_regs += ["ig X", " ig X before"]
        else:
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
        host = self._gather_host_params()
        delay = host.get("Delay Time (s) arb", 0.001)
        for ch_idx, ch_name in [(0, "Arb gain (ch0)"), (1, "Arb gain (ch1)")]:
            target = host.get(f"End value arb (ch{ch_idx})", 0)
            step = host.get(f"Step arb (ch{ch_idx})", 0)
            if step > 0:
                self._ctrl.ramp_register(ch_name, target, step, delay)
                self._append_status(f"Ramping {ch_name} → {target}")

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
            self._wd_trap_low_lbl.setText(f"Low frac: {low:.3f}")
            self._wd_trap_low_lbl.setStyleSheet(
                "color: red; font-size: 10px;" if low < -0.001
                else "color: gray; font-size: 10px;")
        except ValueError:
            self._wd_trap_low_lbl.setText("Low frac: —")

    def _on_wd_generate(self) -> None:
        try:
            n_points = int(self._wd_npoints_combo.currentText())
        except ValueError:
            self._append_status("Invalid n_points.")
            return

        tab_idx = self._wd_type_tabs.currentIndex()

        try:
            if tab_idx == 0:                        # Sine
                n_cycles   = float(self._wd_sine_ncycles.text() or "1.0")
                phase_deg  = float(self._wd_sine_phase.text()   or "0.0")
                result     = generate_sine(n_points, n_cycles, phase_deg)
                self._wd_result = result
                self._on_wd_update_preview(result)

            elif tab_idx == 1:                      # Triangle
                n_cycles  = float(self._wd_tri_ncycles.text()  or "1.0")
                symmetry  = float(self._wd_tri_symmetry.text() or "0.5")
                result    = generate_triangle(n_points, n_cycles, symmetry)
                self._wd_result = result
                self._on_wd_update_preview(result)

            elif tab_idx == 2:                      # Trapezoid
                n_cycles = float(self._wd_trap_ncycles.text() or "1.0")
                rise     = float(self._wd_trap_rise.text()    or "0.1")
                high     = float(self._wd_trap_high.text()    or "0.4")
                fall     = float(self._wd_trap_fall.text()    or "0.1")
                result   = generate_trapezoid(n_points, n_cycles, rise, high, fall)
                self._wd_result = result
                self._on_wd_update_preview(result)

            elif tab_idx == 3:                      # Freq Comb (MC)
                sample_rate = float(self._wd_samplerate_edit.text() or "1e6")
                mode        = self._wd_comb_mode.currentIndex()
                if mode == 0:   # list
                    freqs = [
                        float(x.strip())
                        for x in self._wd_comb_list_edit.text().split(",")
                        if x.strip()
                    ]
                elif mode == 1:   # arange
                    start = float(self._wd_comb_arange_start.text())
                    stop  = float(self._wd_comb_arange_stop.text())
                    step  = float(self._wd_comb_arange_step.text())
                    freqs = list(np.arange(start, stop + step / 2, step))
                else:             # trap fractions
                    trap_f = float(self._wd_comb_trap_freq.text())
                    fracs  = [
                        float(x.strip())
                        for x in self._wd_comb_fracs_edit.text().split(",")
                        if x.strip()
                    ]
                    freqs = [trap_f * f for f in fracs]

                if not freqs:
                    self._append_status("Freq comb: no frequencies specified.")
                    return

                n_trials = int(self._wd_comb_trials.text() or "1000")
                self._wd_mc_stop_flag = [False]
                self._wd_mc_worker = _CombMCWorker(
                    n_points, sample_rate, freqs, n_trials,
                    self._wd_mc_stop_flag)
                self._wd_mc_worker.progress.connect(self._on_wd_mc_progress)
                self._wd_mc_worker.finished.connect(self._on_wd_mc_done)
                self._wd_gen_btn.setEnabled(False)
                self._wd_cancel_btn.show()
                self._wd_mc_progress.setValue(0)
                self._wd_mc_progress.show()
                self._wd_mc_worker.start()

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
        self._wd_result = result
        self._on_wd_update_preview(result)

    def _on_wd_update_preview(self, result) -> None:
        self._wd_rms_lbl.setText(f"{result.rms:.4f}")
        self._wd_crest_lbl.setText(f"{result.crest_factor:.3f}")
        self._wd_desc_lbl.setText(result.description)
        if self._wd_has_plot:
            s = result.samples
            if len(s) > 2000:
                s = s[:: len(s) // 2000]
            self._wd_plot_curve.setData(s)

    def _on_wd_save(self) -> None:
        if self._wd_result is None:
            self._append_status("No waveform generated yet.")
            return
        default_dir = Path(__file__).parent / "waveforms"
        default_dir.mkdir(exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Waveform File",
            str(default_dir / "waveform.txt"),
            "Text files (*.txt);;CSV files (*.csv);;All files (*)",
        )
        if path:
            save_waveform(self._wd_result, path)
            self._arb_file_edit.setText(path)
            self._append_status(
                f"Waveform saved: {path}  "
                f"({self._wd_result.n_points} pts, "
                f"RMS={self._wd_result.rms:.4f})")

    def _on_wd_write_fpga(self) -> None:
        if self._wd_result is None:
            self._append_status("No waveform generated yet.")
            return
        if not self._ctrl.is_connected:
            self._append_status("Not connected.")
            return
        # Write to a temp file so load_arb_waveform can read it
        tmp_fd, tmppath = tempfile.mkstemp(
            suffix=".txt", dir=str(Path(__file__).parent))
        os.close(tmp_fd)
        try:
            save_waveform(self._wd_result, tmppath)
            self._ctrl.load_arb_waveform(tmppath)
            self._append_status(
                f"Wrote to FPGA arb buffer: {self._wd_result.description}")
        except Exception as exc:
            self._append_status(f"Write to FPGA failed: {exc}")
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

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
        widget = QWidget()
        outer  = QHBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)

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

        outer.addLayout(left, 1)

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

        outer.addLayout(right, 1)
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
            _fmt_opt(p.get("speed_pct"),  "d",  " %"))
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
            def run(self): ok = self._c.start_pump(); self.done.emit(ok, "Pump started" if ok else "Start failed")
        w = _W(self._tic_ctrl)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        w.done.connect(lambda *_: self._on_tic_poll())
        w.start()

    def _on_tic_stop_pump(self) -> None:
        if self._tic_ctrl is None:
            return
        class _W(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, ctrl): super().__init__(); self._c = ctrl
            def run(self): ok = self._c.stop_pump(); self.done.emit(ok, "Pump stopped" if ok else "Stop failed")
        w = _W(self._tic_ctrl)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        w.done.connect(lambda *_: self._on_tic_poll())
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
            def run(self): ok = self._c.set_pump_speed(self._p); self.done.emit(ok, f"Speed setpoint → {self._p}%" if ok else "Set speed failed")
        w = _W(self._tic_ctrl, pct)
        w.done.connect(lambda ok, msg: self._append_status(f"TIC: {msg}"))
        w.start()

    # ==================================================================
    # Cleanup
    # ==================================================================

    def closeEvent(self, event) -> None:
        self._tic_timer.stop()
        if self._tic_ctrl is not None:
            self._tic_ctrl.disconnect()
        self._proc_manager.teardown_all()
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
