"""
modules/mod_edwards_tic.py

Hardware module for the Edwards Turbo Instrument Controller (TIC).
Controls the turbomolecular pump and reads WRG / APGX pressure gauges via RS-232.

Communication uses the EDWARDS-TIC library in resources/EDWARDS-TIC.

Module protocol
---------------
  MODULE_NAME   "EDWARDS_TIC"
  DEVICE_NAME   "Edwards TIC (turbo pump + gauges)"
  CONFIG_FIELDS port, baud_rate
  DEFAULTS      wrg_mbar, apgx_mbar, pump_running, pump_speed_pct,
                pump_power_w, pump_temp_c, pump_status_str
  read(config)  connect → read gauges + pump telemetry → disconnect
  test(config)  connect, read, report
  command(config, action, **kwargs)
                start_pump | stop_pump | set_speed
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make EDWARDS-TIC importable without an install
_TIC_DIR = Path(__file__).parent.parent / "resources" / "EDWARDS-TIC"
if str(_TIC_DIR) not in sys.path:
    sys.path.insert(0, str(_TIC_DIR))

try:
    from tic_controller import TICController
    TIC_AVAILABLE = True
except ImportError:
    TIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

MODULE_NAME = "EDWARDS_TIC"
DEVICE_NAME = "Edwards TIC (turbo pump + gauges)"


# ---------------------------------------------------------------------------
# Configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "port",
        "label":   "Serial port",
        "type":    "text",
        "default": "COM3",
        "tooltip": "RS-232 COM port connected to the TIC (e.g. COM3)",
    },
    {
        "key":     "baud_rate",
        "label":   "Baud rate",
        "type":    "text",
        "default": "9600",
        "tooltip": "Must match the TIC front-panel baud rate setting",
    },
]


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

KEY_WRG_MBAR       = "wrg_mbar"
KEY_APGX_MBAR      = "apgx_mbar"
KEY_PUMP_RUNNING   = "pump_running"
KEY_PUMP_SPEED_PCT = "pump_speed_pct"
KEY_PUMP_POWER_W   = "pump_power_w"
KEY_PUMP_CURRENT_A = "pump_current_a"
KEY_PUMP_VOLTAGE_V = "pump_voltage_v"
KEY_PUMP_TEMP_C    = "pump_temp_c"
KEY_PUMP_STATUS    = "pump_status_str"

DEFAULTS: dict = {
    KEY_WRG_MBAR:       0.0,
    KEY_APGX_MBAR:      0.0,
    KEY_PUMP_RUNNING:   0.0,
    KEY_PUMP_SPEED_PCT: 0.0,
    KEY_PUMP_POWER_W:   0.0,
    KEY_PUMP_CURRENT_A: 0.0,
    KEY_PUMP_VOLTAGE_V: 0.0,
    KEY_PUMP_TEMP_C:    0.0,
    KEY_PUMP_STATUS:    "Unknown",
}


# ---------------------------------------------------------------------------
# Plugin interface — read
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Connect to the TIC, read all gauges and pump telemetry, then disconnect.

    Returns
    -------
    dict with keys defined by KEY_* constants above.

    Raises
    ------
    RuntimeError    if the EDWARDS-TIC library is not available
    Exception       on any serial communication error
    """
    if not TIC_AVAILABLE:
        raise RuntimeError(
            "EDWARDS-TIC library not found — check resources/EDWARDS-TIC submodule")

    port     = config.get("port", "COM3")
    baudrate = int(config.get("baud_rate", 9600))

    tic = TICController(port, baudrate=baudrate)
    if not tic.connect():
        raise RuntimeError(f"Could not connect to Edwards TIC on {port}")
    try:
        gs  = tic.read_gauges()
        tel = tic.read_pump()
    finally:
        tic.disconnect()

    return {
        KEY_WRG_MBAR:       gs.wrg.value_mbar  if gs.wrg.ok  else float("nan"),
        KEY_APGX_MBAR:      gs.apgx.value_mbar if gs.apgx.ok else float("nan"),
        KEY_PUMP_RUNNING:   1.0 if tel.is_running   else 0.0,
        KEY_PUMP_SPEED_PCT: float(tel.speed_pct)    if tel.speed_pct  is not None else float("nan"),
        KEY_PUMP_POWER_W:   float(tel.power_w)      if tel.power_w    is not None else float("nan"),
        KEY_PUMP_CURRENT_A: float(tel.current_a)    if tel.current_a  is not None else float("nan"),
        KEY_PUMP_VOLTAGE_V: float(tel.voltage_v)    if tel.voltage_v  is not None else float("nan"),
        KEY_PUMP_TEMP_C:    float(tel.temp_c)       if tel.temp_c     is not None else float("nan"),
        KEY_PUMP_STATUS:    tel.status_str,
    }


# ---------------------------------------------------------------------------
# Plugin interface — test
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """Connect, read one set of readings, report. Safe to call from a worker thread."""
    try:
        vals = read(config)
        wrg  = vals[KEY_WRG_MBAR]
        apgx = vals[KEY_APGX_MBAR]
        wrg_str  = f"{wrg:.3e} mbar"  if wrg  == wrg  else "read error"
        apgx_str = f"{apgx:.3e} mbar" if apgx == apgx else "read error"
        status   = vals[KEY_PUMP_STATUS]
        speed    = vals[KEY_PUMP_SPEED_PCT]
        speed_str = f"{speed:.0f}%" if speed == speed else "—"
        return True, (
            f"OK — WRG: {wrg_str}  |  APGX: {apgx_str}  |  "
            f"Pump: {status} ({speed_str})"
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
    Execute a pump command (connects/disconnects around the command).

    Parameters
    ----------
    config  : standard config dict (port, baud_rate)
    **kwargs:
        action : str — one of "start_pump", "stop_pump", "set_speed"

        For action="set_speed":
            speed_pct : int — target speed as % of full speed (0=default/full)

    Returns
    -------
    dict  ok: bool, message: str
    """
    if not TIC_AVAILABLE:
        return {"ok": False,
                "message": "EDWARDS-TIC library not found — check resources/EDWARDS-TIC submodule"}

    action   = kwargs.get("action", "")
    port     = config.get("port", "COM3")
    baudrate = int(config.get("baud_rate", 9600))

    tic = TICController(port, baudrate=baudrate)
    if not tic.connect():
        return {"ok": False, "message": f"Could not connect to Edwards TIC on {port}"}

    try:
        if action == "start_pump":
            ok = tic.start_pump()
            return {"ok": ok, "message": "Pump start command sent" if ok else "Start command failed"}

        elif action == "stop_pump":
            ok = tic.stop_pump()
            return {"ok": ok, "message": "Pump stop command sent" if ok else "Stop command failed"}

        elif action == "set_speed":
            pct = int(kwargs.get("speed_pct", 0))
            ok  = tic.set_pump_speed(pct)
            return {"ok": ok, "message": f"Speed set to {pct}%" if ok else "Set speed failed"}

        else:
            return {"ok": False,
                    "message": f"Unknown action {action!r}. "
                               "Valid: start_pump, stop_pump, set_speed"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        tic.disconnect()


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys

    cfg = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    if len(_sys.argv) > 1:
        cfg["port"] = _sys.argv[1]

    print(f"Testing {DEVICE_NAME} on {cfg['port']} ...")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
    if not ok:
        _sys.exit(1)
