"""
modules/base.py

Instrument module protocol for usphere-control.

Each hardware module is a plain Python file living in this directory.
The module must expose the attributes and functions described in the
``HardwareModule`` protocol below, and optionally subclass
``ResourcePlugin`` to provide a GUI tab.

This mirrors the device-plugin convention in usphere-daq (daq_edwards_tic.py)
so the two codebases stay consistent.

Lifecycle
---------
1. The GUI discovers modules via discover_hardware_modules().
2. Each module appears on the Modules tab with its CONFIG_FIELDS.
3. The user fills in config, clicks "Test" (calls test()), and if OK the
   module is considered "connected".
4. If the module also subclasses ResourcePlugin, a dedicated GUI tab is
   spawned (create_widget()).  The tab calls the module's read/command
   functions directly via the widget logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from PyQt5.QtWidgets import QWidget


# ---------------------------------------------------------------------------
# HardwareModule protocol (module-level interface — not a class)
# ---------------------------------------------------------------------------
#
# MODULE_NAME   : str
#     Short key used as a config-dict key and in log messages.
#     Must be unique across all loaded modules.  Example: "TIC"
#
# DEVICE_NAME   : str
#     Human-readable label shown in the GUI and status messages.
#
# CONFIG_FIELDS : list[dict]
#     Describes the GUI configuration fields.  Each entry:
#         "key"     : str   — field identifier (config dict key)
#         "label"   : str   — text shown next to the input widget
#         "type"    : str   — one of "text", "int", "float", "bool", "choice"
#         "default" : any   — pre-filled value
#         "choices" : list  — (only for type "choice")
#         "tooltip" : str   — (optional)
#
# DEFAULTS : dict
#     Fallback values returned when the device is unavailable.
#
# read(config: dict) -> dict
#     Open a connection, read state, close, return dict of values.
#     MUST raise on any communication error.
#
# test(config: dict) -> tuple[bool, str]
#     Call read() and return (success, message).  Safe to call from a thread.
#
# command(config: dict, **kwargs) -> dict          [optional]
#     Send a command.  Returns {"ok": bool, "message": str, ...}.


# ---------------------------------------------------------------------------
# ResourcePlugin — optional base class for modules that provide a GUI tab
# ---------------------------------------------------------------------------

class ResourcePlugin(ABC):
    """
    Base class for instrument resources that expose a GUI tab.

    Subclass this alongside your module's plain-function interface when
    the instrument needs its own persistent widget (connection management,
    live readback, parameter controls).

    The FPGA GUI calls create_widget() when the resource is first activated,
    and passes fast-data / slow-poll updates via on_fast_data() /
    on_fpga_update().  teardown() is called on app exit.

    Attributes
    ----------
    NAME        — tab label shown in the GUI
    DESCRIPTION — tooltip / about text
    PERSISTENT  — if True, the tab is always present (not in Procedures list)
    """

    NAME:        str  = "Unnamed Resource"
    DESCRIPTION: str  = ""
    PERSISTENT:  bool = False

    @abstractmethod
    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        """Return the QWidget to embed as a tab. Called once."""

    def on_fast_data(self, values: dict[str, float]) -> None:
        """Called from the FPGA monitor thread with every fast-data packet.

        Route UI updates through Qt signals — never touch widgets directly
        from this method.
        """

    def on_fpga_update(self, state: dict[str, float]) -> None:
        """Called each slow poll cycle (~5 Hz) with full register snapshot."""

    def get_state(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict for ZMQ broadcast / IPC."""
        return {}

    def get_ui_state(self) -> dict:
        """Return widget values for session persistence."""
        return {}

    def restore_ui_state(self, state: dict) -> None:
        """Restore widget values from a session-state dict."""

    def teardown(self) -> None:
        """Stop background threads, release hardware. Safe to call always."""
