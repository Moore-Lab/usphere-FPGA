"""
modules/mod_butterfly_valve.py

Hardware module for the Ideal Vacuum CommandValve butterfly valve on the
pump foreline.  Delegates to resources/IdealVac-CommandValve/.

Position convention (from manual):
  0°  — fully closed (butterfly perpendicular to flow)
  90° — fully open   (butterfly parallel to flow, maximum conductance)

Module protocol
---------------
  MODULE_NAME   "BUTTERFLY_VALVE"
  DEVICE_NAME   "Butterfly Valve (Foreline)"
  CONFIG_FIELDS port
  read(config)  returns position_deg, temp_c, error_code, warning_code
  test(config)  connect, read status, disconnect
  command(config, action, **kwargs)
                open | close | stop | home | set_angle(angle_deg) |
                ramp_to_angle(target_deg, rate_deg_per_s)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SUBMODULE_DIR = Path(__file__).parent.parent / "resources" / "IdealVac-CommandValve"
if _SUBMODULE_DIR.exists() and str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

try:
    from cv_controller import CommandValveController
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False


MODULE_NAME = "BUTTERFLY_VALVE"
DEVICE_NAME = "Butterfly Valve (Foreline)"

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "port",
        "label":   "Serial port",
        "type":    "text",
        "default": "COM14",
        "tooltip": "COM port for the CommandValve (e.g. COM14). Default baud 9600.",
    },
]


def _connect(config: dict) -> "CommandValveController":
    port = config.get("port", "COM14").strip() or "COM14"
    cv = CommandValveController(port)
    if not cv.connect():
        raise RuntimeError(f"CommandValveController.connect() failed on {port}")
    return cv


def read(config: dict) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"error": "cv_controller not available — check submodule"}
    try:
        cv = _connect(config)
        s = cv.read_status()
        cv.disconnect()
        return {
            "position_deg": s.position_deg,
            "temp_c":       s.temp_c,
            "error_code":   s.error_code,
            "warning_code": s.warning_code,
            "is_ok":        s.is_ok,
        }
    except Exception as exc:
        return {"error": str(exc)}


def test(config: dict) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"ok": False, "error": "cv_controller not available"}
    try:
        cv = _connect(config)
        pos = cv.get_position()
        cv.disconnect()
        return {"ok": True, "position_deg": pos}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def command(config: dict, action: str, **kwargs) -> dict:
    if not CONTROLLER_AVAILABLE:
        return {"ok": False, "error": "cv_controller not available"}
    try:
        cv = _connect(config)
        if action == "open":
            ok = cv.open()
        elif action == "close":
            ok = cv.close()
        elif action == "stop":
            ok = cv.stop()
        elif action == "home":
            ok = cv.home()
        elif action == "set_angle":
            angle = float(kwargs.get("angle_deg", 45.0))
            ok = cv.set_angle(angle)
        else:
            cv.disconnect()
            return {"ok": False, "error": f"Unknown action: {action!r}"}
        pos = cv.get_position()
        cv.disconnect()
        return {"ok": ok, "position_deg": pos}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
