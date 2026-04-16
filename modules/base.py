"""
modules/base.py

Instrument module protocol for usphere-control.

Each hardware module is a plain Python file living in this directory.
The module must expose the attributes and functions described below.
This mirrors the device-plugin convention in usphere-daq (daq_edwards_tic.py)
so the two codebases stay consistent.

Required interface
------------------
MODULE_NAME   : str
    Short key used as a config-dict key and in log messages.
    Must be unique across all loaded modules.  Example: "TIC"

DEVICE_NAME   : str
    Human-readable label shown in the GUI and status messages.
    Example: "Edwards TIC"

CONFIG_FIELDS : list[dict]
    Describes the GUI configuration fields for this module.
    Each entry is a dict with the following keys:

        "key"     : str   — field identifier (used as config dict key)
        "label"   : str   — text shown next to the input widget
        "type"    : str   — one of "text", "int", "float", "bool", "choice"
        "default" : any   — value pre-filled in the GUI
        "choices" : list  — (only for type "choice") list of option strings

DEFAULTS : dict
    Fallback values returned when the device is unavailable or read()
    raises.  The procedure runner substitutes these so that a missing
    instrument never hard-blocks a running procedure.

read(config: dict) -> dict
    Open a connection to the device, read its current state, and return
    a dict mapping output key names to values.  MUST raise on any
    communication error — the caller catches exceptions and falls back
    to DEFAULTS.  Should be stateless (open, read, close each call).

test(config: dict) -> tuple[bool, str]
    Call read() and return (success, human-readable message).
    Must be safe to call from a worker thread.
    Used by the GUI "Test" button on the Modules tab.

Optional interface
------------------
command(config: dict, **kwargs) -> dict
    Send a command to the device (e.g., set a setpoint, open a valve).
    Returns a dict with at least {"ok": bool, "message": str}.
    Implement this when the module drives an output as well as reading.

Example skeleton
----------------
    MODULE_NAME  = "MyInstrument"
    DEVICE_NAME  = "My Instrument (Model XYZ)"
    CONFIG_FIELDS = [
        {"key": "port",    "label": "COM port", "type": "text",  "default": "COM1"},
        {"key": "timeout", "label": "Timeout s","type": "float", "default": 2.0},
    ]
    DEFAULTS = {"value": 0.0}

    def read(config: dict) -> dict:
        ...open serial, query, close...
        return {"value": measured_value}

    def test(config: dict) -> tuple[bool, str]:
        try:
            vals = read(config)
            return True, f"OK — value: {vals['value']}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
"""
