"""
modules/mod_tenma_psu.py

Hardware module for the TENMA 72-XXXX series DC power supply.

Used during Stage 5 (dropper shaking) to set the piezo drive voltage.
The supply is connected via USB-to-serial.  The AWG controls a MOSFET gate
that lets the supply voltage through to the piezo; the supply itself stays
on throughout the shaking sequence.

Driver location
---------------
The TENMA driver lives in the git submodule at:
  resources/TENMA_72-XXXX/
This module inserts that path into sys.path at load time so that
``tenma_controller.TENMAController`` can be imported without a package install.

Module protocol
---------------
  MODULE_NAME   "TENMA_PSU"
  DEVICE_NAME   "TENMA 72-XXXX DC Power Supply"
  CONFIG_FIELDS serial_port, baud_rate
  read(config)  connect, query set-point + output readings, disconnect
  test(config)  connect, report IDN + voltage/current, disconnect
  command(config, action, **kwargs)
                set_voltage | output_on | output_off | get_voltage
  open_psu(config)
                context manager — yields a connected TENMAController
                for use in long-running worker threads

Safety cap
----------
MAX_VOLTAGE_V = 60.0 V.  set_voltage() raises ValueError above this limit.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the submodule driver is importable
# ---------------------------------------------------------------------------

_SUBMODULE_DIR = Path(__file__).parent.parent / "resources" / "TENMA_72-XXXX"
if _SUBMODULE_DIR.exists() and str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

try:
    from tenma_controller import TENMAController
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

MODULE_NAME = "TENMA_PSU"
DEVICE_NAME = "TENMA 72-XXXX DC Power Supply"

MAX_VOLTAGE_V = 60.0


# ---------------------------------------------------------------------------
# Configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "serial_port",
        "label":   "Serial port",
        "type":    "text",
        "default": "COM5",
        "tooltip": (
            "COM port for the TENMA supply (e.g. COM5). "
            "Use 'auto' to probe all ports and connect to the first TENMA found."
        ),
    },
    {
        "key":     "baud_rate",
        "label":   "Baud rate",
        "type":    "int",
        "default": 9600,
        "tooltip": "Serial baud rate — factory default is 9600.",
    },
]


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

KEY_VOLTAGE_SET = "voltage_set_v"
KEY_VOLTAGE_OUT = "voltage_out_v"
KEY_CURRENT_OUT = "current_out_a"
KEY_OUTPUT_ON   = "output_on"

DEFAULTS: dict = {
    KEY_VOLTAGE_SET: 0.0,
    KEY_VOLTAGE_OUT: 0.0,
    KEY_CURRENT_OUT: 0.0,
    KEY_OUTPUT_ON:   0.0,
}


# ---------------------------------------------------------------------------
# Context manager for persistent connections (used by shake worker)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def open_psu(config: dict):
    """
    Yield a connected TENMAController; disconnect on exit.

    Intended for long-running worker threads that need to hold the
    serial connection open across many voltage set calls.

    Parameters
    ----------
    config : dict with "serial_port" and optionally "baud_rate"

    Raises
    ------
    RuntimeError    if the driver submodule is not present
    ConnectionError if the device cannot be reached
    """
    if not CONTROLLER_AVAILABLE:
        raise RuntimeError(
            "TENMA driver not found. "
            f"Expected submodule at: {_SUBMODULE_DIR}"
        )

    port = config.get("serial_port", CONFIG_FIELDS[0]["default"]).strip()
    baud = int(config.get("baud_rate", 9600))
    port_arg = None if port.lower() == "auto" else port

    ctrl = TENMAController(port=port_arg, baud_rate=baud)
    connected = ctrl.connect(port=port_arg, baud_rate=baud)
    if not connected:
        raise ConnectionError(
            f"Could not connect to TENMA PSU on '{port}'. "
            "Check COM port and USB cable."
        )
    try:
        yield ctrl
    finally:
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Plugin interface — read
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """Connect, query voltage/current/output status, disconnect."""
    with open_psu(config) as ctrl:
        v_set = ctrl.get_voltage_set() or 0.0
        v_out = ctrl.get_voltage_out() or 0.0
        i_out = ctrl.get_current_out() or 0.0
        on    = ctrl.is_output_on()
    return {
        KEY_VOLTAGE_SET: float(v_set),
        KEY_VOLTAGE_OUT: float(v_out),
        KEY_CURRENT_OUT: float(i_out),
        KEY_OUTPUT_ON:   1.0 if on else 0.0,
    }


# ---------------------------------------------------------------------------
# Plugin interface — test
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """Connect, report IDN + readings.  Safe to call from a worker thread."""
    try:
        with open_psu(config) as ctrl:
            idn   = ctrl.idn or "?"
            v_set = ctrl.get_voltage_set()
            v_out = ctrl.get_voltage_out()
            i_out = ctrl.get_current_out()
            on    = ctrl.is_output_on()
        v_set_str = f"{v_set:.2f} V"  if v_set is not None else "?"
        v_out_str = f"{v_out:.2f} V"  if v_out is not None else "?"
        i_out_str = f"{i_out:.3f} A"  if i_out is not None else "?"
        out_str   = "ON" if on else "OFF"
        return True, (
            f"OK — {idn}  |  set: {v_set_str}  "
            f"out: {v_out_str} @ {i_out_str}  output: {out_str}"
        )
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Plugin interface — command
# ---------------------------------------------------------------------------

def command(config: dict, **kwargs) -> dict:
    """
    Execute a PSU command.  Opens a connection, runs the command, disconnects.

    Parameters
    ----------
    config : standard config dict
    **kwargs:
        action : str — one of "set_voltage", "output_on", "output_off",
                        "get_voltage"

        For action="set_voltage":
            voltage_v : float — target voltage in volts (capped at MAX_VOLTAGE_V)

    Returns
    -------
    dict
        ok        : bool
        message   : str
        voltage_v : float (current set-point; 0.0 if unavailable)
        output_on : bool
    """
    _empty = {"ok": False, "message": "", "voltage_v": 0.0, "output_on": False}

    if not CONTROLLER_AVAILABLE:
        return {**_empty, "message": "TENMA driver not found — check submodule"}

    action = kwargs.get("action", "")

    try:
        with open_psu(config) as ctrl:

            if action == "set_voltage":
                v = float(kwargs["voltage_v"])
                if v > MAX_VOLTAGE_V:
                    return {
                        **_empty,
                        "message": (
                            f"Voltage {v:.2f} V exceeds safety limit "
                            f"({MAX_VOLTAGE_V:.0f} V)"
                        ),
                    }
                ok = ctrl.set_voltage(v)
                return {
                    "ok":        ok,
                    "message":   f"Voltage set to {v:.2f} V" if ok else "set_voltage failed",
                    "voltage_v": v if ok else 0.0,
                    "output_on": ctrl.is_output_on(),
                }

            elif action == "output_on":
                ok = ctrl.output_on()
                return {
                    "ok":        ok,
                    "message":   "Output ON" if ok else "output_on failed",
                    "voltage_v": ctrl.get_voltage_set() or 0.0,
                    "output_on": ok,
                }

            elif action == "output_off":
                ok = ctrl.output_off()
                return {
                    "ok":        ok,
                    "message":   "Output OFF" if ok else "output_off failed",
                    "voltage_v": ctrl.get_voltage_set() or 0.0,
                    "output_on": False,
                }

            elif action == "get_voltage":
                v_set = ctrl.get_voltage_set()
                return {
                    "ok":        v_set is not None,
                    "message":   f"Set: {v_set:.2f} V" if v_set is not None else "get_voltage failed",
                    "voltage_v": float(v_set) if v_set is not None else 0.0,
                    "output_on": ctrl.is_output_on(),
                }

            else:
                return {
                    **_empty,
                    "message": (
                        f"Unknown action {action!r}. "
                        "Valid actions: set_voltage, output_on, output_off, get_voltage"
                    ),
                }

    except KeyError as exc:
        return {**_empty, "message": f"Missing parameter: {exc}"}
    except ValueError as exc:
        return {**_empty, "message": str(exc)}
    except Exception as exc:
        return {**_empty, "message": f"{type(exc).__name__}: {exc}"}
