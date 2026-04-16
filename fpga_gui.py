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
import sys
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
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
    QPushButton,
    QScrollArea,
    QSpinBox,
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


def _fmt(v: float) -> str:
    if v != 0 and (abs(v) < 0.001 or abs(v) >= 1e6):
        return f"{v:.6e}"
    return f"{v:.6f}".rstrip("0").rstrip(".")


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
# Main window
# ---------------------------------------------------------------------------

class FPGAMainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("usphere — FPGA Control")
        self.resize(1400, 900)

        icon_path = Path(__file__).parent / "assets" / "Logo_transparent_outlined.PNG"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Signal bridge for thread safety
        self._signals = _Signals()
        self._signals.status_message.connect(self._append_status)
        self._signals.registers_updated.connect(self._on_registers_updated)
        self._signals.plot_data.connect(self._on_plot_data)
        self._signals.connected.connect(self._on_connected)
        self._signals.disconnected.connect(self._on_disconnected)

        # Backend controller
        self._ctrl = FPGAController(
            on_status=self._signals.status_message.emit,
            on_registers_updated=self._signals.registers_updated.emit,
            on_plot_data=self._signals.plot_data.emit,
            on_connected=self._signals.connected.emit,
            on_disconnected=self._signals.disconnected.emit,
        )

        # Widget maps (populated during build)
        self._reg_edits: dict[str, QLineEdit] = {}   # FPGA register widgets
        self._host_edits: dict[str, QLineEdit] = {}   # host-param widgets
        self._host_values: dict[str, float] = dict(HOST_PARAM_DEFAULTS)
        self._bead_fb_combos: dict[str, QComboBox] = {}  # per-axis bead fb selector
        self._boost_multiplier: float = 10.0              # boost gain factor

        self._build_ui()
        self._restore_session()

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        tabs.addTab(self._build_connection_tab(), "Connection")
        tabs.addTab(self._build_pid_tab("X"), "X Feedback")
        tabs.addTab(self._build_pid_tab("Y"), "Y Feedback")
        tabs.addTab(self._build_pid_tab("Z"), "Z Feedback")
        tabs.addTab(self._build_waveform_tab(), "Waveform")
        tabs.addTab(self._build_outputs_tab(), "Outputs")
        tabs.addTab(self._build_registers_tab(), "All Registers")
        tabs.addTab(self._build_plot_tab(), "Monitor")

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
    # PID feedback tab (one per axis: X, Y, Z)
    # ------------------------------------------------------------------

    def _build_pid_tab(self, axis: str) -> QWidget:
        """Build PID controls tab for axis X, Y, or Z."""
        widget = QWidget()
        outer = QVBoxLayout(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)

        a = axis.upper()
        al = axis.lower()

        # ---------- Bead Feedback (after chamber) ----------
        bead_grp = QGroupBox(f"Bead Feedback — {a} Axis")
        bg = QGridLayout()
        r = 0
        # Bead feedback selector (host-side only — stored for reference)
        bg.addWidget(QLabel("Bead feedback:"), r, 0)
        fb_combo = QComboBox()
        fb_combo.addItems(["Normal", "Inverted"])
        self._bead_fb_combos[a] = fb_combo
        bg.addWidget(fb_combo, r, 1)
        r += 1
        r = self._add_reg(bg, r, f"{a} Setpoint")
        r = self._add_reg(bg, r, f"DC offset {a}")
        r = self._add_reg(bg, r, f"pg {a}")
        ig_name = f" ig {a}" if a != "X" else f"ig {a}"
        r = self._add_reg(bg, r, ig_name)
        r = self._add_reg(bg, r, f"dg {a}")
        r = self._add_reg(bg, r, f"dg band {a}")
        r = self._add_reg(bg, r, f"pg {a} mod" if a == "Z" else f"dg{al} mod")
        r = self._add_reg(bg, r, f"Upper lim {a}")
        r = self._add_reg(bg, r, f"Lower lim {a}")
        # Indicators
        for ind in [f"AI {a} plot", f"fb {a} plot", f"tot_fb {a} plot"]:
            r = self._add_reg(bg, r, ind)
        bead_grp.setLayout(bg)
        layout.addWidget(bead_grp)

        # ---------- Before-chamber PID ----------
        before_grp = QGroupBox(
            f"AOM Feedback — Before Chamber" if a == "Z"
            else f"Before-Chamber PID — {a}")
        bg2 = QGridLayout()
        r = 0
        r = self._add_reg(bg2, r, f"Use {a} PID before")
        r = self._add_reg(bg2, r, f"{a} before Setpoint")
        r = self._add_reg(bg2, r, f"pg {a} before")
        ig_before = f" ig {a} before"
        r = self._add_reg(bg2, r, ig_before)
        r = self._add_reg(bg2, r, f"dg {a} before")
        r = self._add_reg(bg2, r, f"dg band {a} before")
        r = self._add_reg(bg2, r, f"Upper lim {a} before")
        r = self._add_reg(bg2, r, f"Lower lim {a} before")
        # Before indicators
        for ind in [f"AI {a} before chamber plot",
                     f"fb {a} before chamber plot"]:
            r = self._add_reg(bg2, r, ind)
        before_grp.setLayout(bg2)
        layout.addWidget(before_grp)

        # ---------- Miscellaneous axis controls ----------
        misc_grp = QGroupBox(f"Misc — {a}")
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

        # ---------- Filter Parameters (host-side freq/Q) ----------
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

        # ---------- Notch Filters (host-side) ----------
        notch_grp = QGroupBox("Notch Filters")
        ng = QGridLayout()
        r = 0
        for i in range(1, 5):
            r = self._add_host(ng, r, f"notch freq {i} {al}")
            r = self._add_host(ng, r, f"notch Q {i} {al}")
        notch_grp.setLayout(ng)
        layout.addWidget(notch_grp)

        # ---------- Computed coefficients (read-only display) ----------
        coeff_grp = QGroupBox("Computed Coefficients (FPGA Registers)")
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
        change_btn = QPushButton(
            "Change Pars 2" if a == "Z" else "Change Pars")
        change_btn.setStyleSheet("font-weight: bold; padding: 8px;")
        change_btn.clicked.connect(lambda _, ax=a: self._on_change_pars(ax))
        btn_layout.addWidget(change_btn)

        reset_btn = QPushButton(f"Reset {al} accum")
        reset_btn.clicked.connect(
            lambda _, n=f"Reset {al} accum": self._write_one_value(n, 1.0))
        btn_layout.addWidget(reset_btn)

        save_sph_btn = QPushButton("Save Sphere")
        save_sph_btn.clicked.connect(self._on_save_sphere)
        btn_layout.addWidget(save_sph_btn)

        boost_btn = QPushButton("Boost")
        boost_btn.setStyleSheet("background-color: #ff9800; font-weight: bold; padding: 8px;")
        boost_btn.clicked.connect(lambda _, ax=a: self._on_boost(ax))
        btn_layout.addWidget(boost_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)
        return widget

    # ------------------------------------------------------------------
    # Waveform tab
    # ------------------------------------------------------------------

    def _build_waveform_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

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
        return widget

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
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)

        # Settings row
        top = QHBoxLayout()
        top.addWidget(QLabel("Plot poll interval:"))
        self._plot_poll_spin = QSpinBox()
        self._plot_poll_spin.setRange(5, 1000)
        self._plot_poll_spin.setValue(self._ctrl.config.plot_interval_ms)
        self._plot_poll_spin.setSuffix(" ms")
        top.addWidget(self._plot_poll_spin)
        top.addStretch()
        layout.addLayout(top)

        # Embedded 3×3 plot grid
        self._plot_widget = FPGAPlotWidget()
        layout.addWidget(self._plot_widget, stretch=1)
        return widget

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
        # Reuse existing widget if already created (shared across tabs)
        if name in self._reg_edits:
            edit = _make_edit(0.0, readonly=ro, width=w)
        else:
            edit = _make_edit(0.0, readonly=ro, width=w)
        self._reg_edits.setdefault(name, edit)
        grid.addWidget(edit, row, 1)
        if not ro:
            btn = QPushButton("Set")
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _, n=name, e=edit: self._write_one_edit(n, e))
            grid.addWidget(btn, row, 2)
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
        self._update_reg_edits(values)
        self._append_status(f"Read {len(values)} registers")

    def _on_registers_updated(self, values: dict) -> None:
        self._update_reg_edits(values)
        self._proc_manager.notify_fpga_update(values)

    def _on_plot_data(self, values: dict) -> None:
        self._plot_widget.push_values(values)

    def _update_reg_edits(self, values: dict[str, float]) -> None:
        for name, val in values.items():
            edit = self._reg_edits.get(name)
            if edit is not None:
                edit.setText(_fmt(val))

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
    # Cleanup
    # ==================================================================

    def closeEvent(self, event) -> None:
        self._proc_manager.teardown_all()
        self._ctrl.disconnect()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = FPGAMainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
