"""
procedures/base.py

Base class for usphere-control automation procedures.

Analogous to plugins/base.py in usphere-daq, but for instrument control
rather than data analysis.  A procedure orchestrates one or more hardware
modules (from modules/) and FPGA registers to carry out a discrete step in
the sphere trapping and preparation protocol.

Each procedure is a Python module living in this directory.  The module
must expose a class called ``Procedure`` that subclasses ``ControlProcedure``.
The procedure manager discovers it by name and injects the FPGAFacade.

FPGAFacade
----------
A thin interface to the live FPGAController (fpga_core.py).  The procedure
receives a concrete FPGAFacade instance via the ``fpga`` class attribute
after loading.  This keeps procedures decoupled from the GUI and testable in
isolation (swap in a mock FPGAFacade).

ControlProcedure callbacks
--------------------------
on_fpga_update(state)
    Called each monitor cycle (~200 ms by default) with the current dict of
    all FPGA register values.  Analogous to on_file_written() in DAQ analysis
    plugins.  Use this for live polling: trap detection, pressure checks,
    charge readback, etc.  Always runs on the monitor thread — route UI
    updates through Qt signals.
"""

from __future__ import annotations

import threading
from PyQt5.QtWidgets import QWidget


# ---------------------------------------------------------------------------
# FPGA facade
# ---------------------------------------------------------------------------

class FPGAFacade:
    """
    Thin interface to FPGAController, injected into every ControlProcedure.

    The real implementation is fpga_core.FPGAController — this class exists
    so procedures can be written and tested without importing fpga_core
    directly (mock subclasses can be used in unit tests).

    All methods mirror the FPGAController public API exactly.
    """

    # --- Register I/O ---

    def read_register(self, name: str) -> float:
        """Read a single FPGA register by name.  Returns 0.0 on failure."""
        raise NotImplementedError

    def write_register(self, name: str, value: float) -> None:
        """Write a single FPGA register.  Raises on unknown / read-only name."""
        raise NotImplementedError

    def read_registers(self, names: list[str]) -> dict[str, float]:
        """Read a subset of registers.  Returns {name: value}."""
        raise NotImplementedError

    def read_all(self) -> dict[str, float]:
        """Read every register.  Returns {name: value}."""
        raise NotImplementedError

    def write_many(self, values: dict[str, float]) -> dict[str, str]:
        """Write multiple registers.  Returns {name: error_msg} for failures."""
        raise NotImplementedError

    # --- Compound operations ---

    def ramp_register(
        self,
        name: str,
        target: float,
        step: float,
        delay_s: float,
        callback=None,
    ) -> threading.Thread:
        """Ramp a register to *target* in a background thread.

        Parameters
        ----------
        name    : register name
        target  : final value
        step    : absolute step size (sign determined automatically)
        delay_s : seconds between steps
        callback: optional callable(current_value) after each step

        Returns the started daemon thread.
        """
        raise NotImplementedError

    def change_pars(
        self,
        axis: str,
        host_params: dict[str, float],
        pid_values: dict[str, float] | None = None,
    ) -> dict[str, str]:
        """Compute filter coefficients from host params and write to FPGA.

        axis        : "X", "Y", or "Z"
        host_params : freq/Q values (same format as the GUI host-params dict)
        pid_values  : optional additional register values to write

        Returns {name: error_msg} for any failures.
        """
        raise NotImplementedError

    # --- State ---

    @property
    def is_connected(self) -> bool:
        """True if the FPGA session is open (or simulation mode is active)."""
        raise NotImplementedError

    @property
    def is_simulated(self) -> bool:
        """True when running without real hardware (nifpga not installed)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Procedure base class
# ---------------------------------------------------------------------------

class ControlProcedure:
    """
    Interface that every control procedure must implement.

    Subclass this, set NAME and DESCRIPTION, implement create_widget(), and
    expose the subclass as ``Procedure`` in the module so the discovery
    machinery can find it.

    The procedure manager injects a live FPGAFacade into ``fpga`` after
    loading, then calls on_fpga_update() each monitor cycle.
    """

    NAME: str = "Unnamed Procedure"
    DESCRIPTION: str = ""

    fpga: FPGAFacade | None = None
    """Injected by the procedure manager after loading."""

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        """Return the QWidget to embed as a new tab in the FPGA GUI."""
        raise NotImplementedError

    def on_fpga_update(self, state: dict[str, float]) -> None:
        """Called each monitor cycle with the current FPGA register snapshot.

        *state* is the same dict returned by FPGAFacade.read_all().
        Override to react to live hardware state (trap signal crossing a
        threshold, pressure reaching a target, etc.).

        This is called from the monitor thread — never update Qt widgets
        directly here.  Emit a Qt signal instead.
        """

    def teardown(self) -> None:
        """Called when the procedure tab is closed or the app shuts down.

        Stop background threads, release serial ports, disable hardware
        outputs.  Must be safe to call even if create_widget() was never
        called.
        """


# ---------------------------------------------------------------------------
# Live FPGAFacade adapter (wraps the real FPGAController)
# ---------------------------------------------------------------------------

class LiveFPGAFacade(FPGAFacade):
    """Wraps a real fpga_core.FPGAController instance.

    Instantiated by the FPGA GUI and passed to every ControlProcedure.
    Procedures never import fpga_core directly — they receive this object.
    """

    def __init__(self, controller) -> None:
        # controller is a fpga_core.FPGAController instance
        self._ctrl = controller

    def read_register(self, name: str) -> float:
        return self._ctrl.read_register(name)

    def write_register(self, name: str, value: float) -> None:
        self._ctrl.write_register(name, value)

    def read_registers(self, names: list[str]) -> dict[str, float]:
        return self._ctrl.read_registers(names)

    def read_all(self) -> dict[str, float]:
        return self._ctrl.read_all()

    def write_many(self, values: dict[str, float]) -> dict[str, str]:
        return self._ctrl.write_many(values)

    def ramp_register(self, name, target, step, delay_s, callback=None):
        return self._ctrl.ramp_register(name, target, step, delay_s, callback)

    def change_pars(self, axis, host_params, pid_values=None):
        return self._ctrl.change_pars(axis, host_params, pid_values)

    @property
    def is_connected(self) -> bool:
        return self._ctrl.is_connected

    @property
    def is_simulated(self) -> bool:
        return self._ctrl.is_simulated
