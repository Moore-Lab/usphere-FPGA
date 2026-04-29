"""
modules/mod_solenoid_valve.py

Hardware module for the N₂ solenoid valve on the pump foreline tee.
Delegates to resources/Solenoid-valve-controller/valve_controller.py.

Module protocol
---------------
  MODULE_NAME   "SOLENOID_VALVE"
  DEVICE_NAME   "N₂ Solenoid Valve"
  CONFIG_FIELDS port
  read(config)  returns local open/closed state (no hardware query)
  test(config)  connect and disconnect to verify port
  command(config, action, **kwargs)
                open | close | pulse(duration_s)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SUBMODULE_DIR = Path(__file__).parent.parent / "resources" / "Solenoid-valve-controller"
if _SUBMODULE_DIR.exists() and str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

try:
    from valve_controller import ValveController
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False


MODULE_NAME = "SOLENOID_VALVE"
DEVICE_NAME = "N₂ Solenoid Valve"

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "port",
        "label":   "Serial port",
        "type":    "text",
        "default": "",
        "tooltip": "COM port for the solenoid valve controller (e.g. COM5). Leave blank to auto-detect.",
    },
]


def read(config: dict) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"error": "valve_controller not available — check submodule"}
    port = config.get("port", "").strip() or None
    try:
        vc = ValveController(port)
        s = vc.status()
        vc.disconnect()
        return s
    except Exception as exc:
        return {"error": str(exc)}


def test(config: dict) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"ok": False, "error": "valve_controller not available"}
    port = config.get("port", "").strip() or None
    try:
        vc = ValveController(port)
        vc.disconnect()
        return {"ok": True, "port": port or "auto-detected"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def command(config: dict, action: str, **kwargs) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"ok": False, "error": "valve_controller not available"}
    port = config.get("port", "").strip() or None
    try:
        vc = ValveController(port)
        if action == "open":
            vc.open()
        elif action == "close":
            vc.close()
        elif action == "pulse":
            duration_s = float(kwargs.get("duration_s", 1.0))
            vc.pulse(duration_s)
        else:
            vc.disconnect()
            return {"ok": False, "error": f"Unknown action: {action!r}"}
        result = vc.status()
        vc.disconnect()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
