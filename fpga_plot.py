"""
fpga_plot.py

Real-time diagnostic plot windows for FPGA feedback signals.

Three independent top-level windows (X, Y, Z) show scrolling time-domain
traces of the last 5 seconds.  PSD is computed on demand via a button
(not live-updated).

Drawing is decoupled from data arrival: data is buffered on push and
a QTimer redraws at ~30 fps using line.set_data() for efficiency.
"""

from __future__ import annotations

import collections
import time

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ── Channel definitions per axis ─────────────────────────────────────────

AXIS_CHANNELS: dict[str, list[dict]] = {
    "X": [
        {"name": "AI X plot",               "label": "AI X",        "color": "#9467bd"},
        {"name": "AI X before chamber plot", "label": "AI X before", "color": "#c5b0d5"},
        {"name": "fb X plot",               "label": "fb X",        "color": "#8c564b"},
        {"name": "fb X before chamber plot", "label": "fb X before", "color": "#c49c94"},
        {"name": "tot_fb X plot",           "label": "tot fb X",    "color": "#7b4173"},
    ],
    "Y": [
        {"name": "AI Y plot",               "label": "AI Y",        "color": "#2ca02c"},
        {"name": "AI Y before chamber plot", "label": "AI Y before", "color": "#98df8a"},
        {"name": "fb Y plot",               "label": "fb Y",        "color": "#d62728"},
        {"name": "fb Y before chamber plot", "label": "fb Y before", "color": "#ff9896"},
        {"name": "tot_fb Y plot",           "label": "tot fb Y",    "color": "#a63603"},
    ],
    "Z": [
        {"name": "AI Z plot",               "label": "AI Z",        "color": "#1f77b4"},
        {"name": "AI Z before chamber plot", "label": "AI Z before", "color": "#aec7e8"},
        {"name": "fb Z plot",               "label": "fb Z",        "color": "#ff7f0e"},
        {"name": "fb Z before chamber plot", "label": "fb Z before", "color": "#ffbb78"},
        {"name": "tot_fb Z plot",           "label": "tot fb Z",    "color": "#e6550d"},
        {"name": "accum out z1",            "label": "Accum Z1",    "color": "#17becf"},
        {"name": "accum out z2",            "label": "Accum Z2",    "color": "#bcbd22"},
    ],
}

# Flat list of every plot register name (exported for fpga_core fast reads)
ALL_PLOT_NAMES: list[str] = []
for _chs in AXIS_CHANNELS.values():
    for _ch in _chs:
        if _ch["name"] not in ALL_PLOT_NAMES:
            ALL_PLOT_NAMES.append(_ch["name"])


# ── PSD dialog (on-demand, not live) ─────────────────────────────────────

class PSDDialog(QDialog):
    """One-shot PSD computed from a snapshot of the current ring buffer."""

    def __init__(self, axis: str, times: list[float],
                 data: dict[str, list[float]],
                 channels: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"PSD — {axis}")
        self.resize(750, 420)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._times = np.asarray(times)
        self._data = {k: np.asarray(v) for k, v in data.items()}
        self._channels = channels

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Window:"))
        self._win_combo = QComboBox()
        self._win_combo.addItems(["hanning", "hamming", "blackman", "boxcar"])
        self._win_combo.currentIndexChanged.connect(self._recompute)
        top.addWidget(self._win_combo)
        top.addStretch()
        layout.addLayout(top)

        self._fig = Figure(figsize=(7, 3.5), tight_layout=True)
        self._ax = self._fig.add_subplot(111)
        self._canvas = FigureCanvas(self._fig)
        layout.addWidget(self._canvas)

        self._recompute()

    def _recompute(self) -> None:
        ax = self._ax
        ax.clear()
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("PSD")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.grid(True, alpha=0.3, which="both")

        n = len(self._times)
        if n < 8:
            self._canvas.draw()
            return

        dt = (self._times[-1] - self._times[0]) / (n - 1)
        fs = 1.0 / dt if dt > 0 else 100.0

        win_name = self._win_combo.currentText()
        win_fn = {"hanning": np.hanning, "hamming": np.hamming,
                  "blackman": np.blackman}.get(win_name)
        win = win_fn(n) if win_fn else np.ones(n)
        win_norm = np.sum(win ** 2)
        if win_norm == 0:
            self._canvas.draw()
            return

        for ch in self._channels:
            vs = self._data.get(ch["name"])
            if vs is None or len(vs) != n:
                continue
            windowed = (vs - np.mean(vs)) * win
            fft_vals = np.fft.rfft(windowed)
            psd = (2.0 / (fs * win_norm)) * np.abs(fft_vals) ** 2
            freqs = np.fft.rfftfreq(n, d=1.0 / fs)
            if len(freqs) > 1:
                ax.plot(freqs[1:], psd[1:], label=ch["label"],
                        color=ch["color"], linewidth=1)

        ax.legend(fontsize=7, loc="upper right")
        self._canvas.draw()


# ── Per-axis live plot window ─────────────────────────────────────────────

class LiveAxisWindow(QMainWindow):
    """Scrolling time-domain strip chart for one feedback axis."""

    def __init__(self, axis: str, history_sec: float = 5.0):
        super().__init__()  # no parent → independent top-level window
        self.setWindowTitle(f"{axis} Axis — Live")
        self.resize(900, 300)

        self._axis = axis
        self._history_sec = history_sec
        self._channels = AXIS_CHANNELS[axis]
        self._t0 = time.monotonic()
        self._allow_close = False  # True only on app shutdown

        # Ring buffers — generous size for 5 s at up to 1 kHz poll rate
        self._max_points = 5000
        self._times: collections.deque[float] = collections.deque(maxlen=self._max_points)
        self._data: dict[str, collections.deque] = {
            ch["name"]: collections.deque(maxlen=self._max_points)
            for ch in self._channels
        }

        self._build_ui()

        # Timer-driven redraw (~30 fps), starts on first showEvent
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._redraw)

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        w = QWidget()
        self.setCentralWidget(w)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        # Checkboxes + PSD button
        top = QHBoxLayout()
        self._cbs: dict[str, QCheckBox] = {}
        for i, ch in enumerate(self._channels):
            cb = QCheckBox(ch["label"])
            cb.setChecked(i < 2)  # first two channels on by default
            cb.stateChanged.connect(self._on_cb)
            self._cbs[ch["name"]] = cb
            top.addWidget(cb)
        top.addStretch()
        psd_btn = QPushButton("Compute PSD")
        psd_btn.clicked.connect(self._show_psd)
        top.addWidget(psd_btn)
        layout.addLayout(top)

        # Matplotlib canvas (no toolbar — keep lightweight)
        self._fig = Figure(figsize=(9, 2.5), tight_layout=True)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_xlabel("Time (s)")
        self._ax.set_ylabel("Value")
        self._ax.grid(True, alpha=0.3)
        self._canvas = FigureCanvas(self._fig)
        layout.addWidget(self._canvas)

        # Pre-create Line2D objects (avoids re-creation on every frame)
        self._lines: dict[str, object] = {}
        for ch in self._channels:
            (line,) = self._ax.plot([], [], label=ch["label"],
                                    color=ch["color"], linewidth=1)
            self._lines[ch["name"]] = line
            if not self._cbs[ch["name"]].isChecked():
                line.set_visible(False)

        self._ax.legend(fontsize=7, loc="upper left")
        self._canvas.draw()

    # ── Checkbox toggle ───────────────────────────────────────────────

    def _on_cb(self) -> None:
        for ch in self._channels:
            self._lines[ch["name"]].set_visible(
                self._cbs[ch["name"]].isChecked())
        self._dirty = True

    # ── Data input (called from PlotManager) ──────────────────────────

    def push_values(self, values: dict[str, float]) -> None:
        """Buffer a new sample.  No drawing happens here."""
        t = time.monotonic() - self._t0
        self._times.append(t)
        for ch in self._channels:
            self._data[ch["name"]].append(values.get(ch["name"], 0.0))
        self._dirty = True

    # ── Timer-driven redraw ───────────────────────────────────────────

    def _redraw(self) -> None:
        if not self._dirty or not self._times:
            return
        self._dirty = False

        t_arr = np.asarray(self._times)
        t_now = t_arr[-1]
        t_min = t_now - self._history_sec

        mask = t_arr >= t_min
        t_vis = t_arr[mask]

        has_data = False
        for ch in self._channels:
            line = self._lines[ch["name"]]
            if not line.get_visible():
                line.set_data([], [])
                continue
            v_arr = np.asarray(self._data[ch["name"]])
            line.set_data(t_vis, v_arr[mask])
            has_data = True

        if has_data and len(t_vis):
            self._ax.set_xlim(t_min, t_now)
            self._ax.relim()
            self._ax.autoscale_view(scalex=False, scaley=True)

        self._canvas.draw_idle()

    # ── On-demand PSD ─────────────────────────────────────────────────

    def _show_psd(self) -> None:
        if len(self._times) < 8:
            return
        active = [ch for ch in self._channels
                  if self._cbs[ch["name"]].isChecked()]
        if not active:
            return
        times = list(self._times)
        data = {ch["name"]: list(self._data[ch["name"]]) for ch in active}
        dlg = PSDDialog(self._axis, times, data, active, parent=self)
        dlg.show()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def clear(self) -> None:
        self._times.clear()
        for d in self._data.values():
            d.clear()
        self._t0 = time.monotonic()
        self._dirty = True

    def showEvent(self, event) -> None:
        if not self._timer.isActive():
            self._timer.start(33)  # ~30 fps
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        if self._allow_close:
            self._timer.stop()
            event.accept()
        else:
            # Just hide — re-show via "Show Plot Windows"
            event.ignore()
            self.hide()


# ── Plot manager ──────────────────────────────────────────────────────────

class PlotManager:
    """Creates and manages the three per-axis live plot windows."""

    def __init__(self) -> None:
        self._windows: dict[str, LiveAxisWindow] = {
            axis: LiveAxisWindow(axis) for axis in ("X", "Y", "Z")
        }

    def show(self) -> None:
        for w in self._windows.values():
            w.show()
            w.raise_()

    def hide(self) -> None:
        for w in self._windows.values():
            w.hide()

    def push_values(self, values: dict[str, float]) -> None:
        for w in self._windows.values():
            w.push_values(values)

    def clear(self) -> None:
        for w in self._windows.values():
            w.clear()

    def close(self) -> None:
        """Permanently close all plot windows (app shutdown)."""
        for w in self._windows.values():
            w._allow_close = True
            w.close()


# ── Standalone mode ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    mgr = PlotManager()
    mgr.show()
    sys.exit(app.exec_())
