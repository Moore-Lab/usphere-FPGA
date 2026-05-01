"""
modules/mod_dropper_stage.py

Hardware module for the dropper translation stage:
  Thorlabs Z812 linear actuator + KDC101 KCube DC Servo Motor Controller.

Core logic lives in the git submodule at:
  resources/kcube-motor-controller/

This module is a lightweight wrapper that:
  1. Adds the submodule to sys.path so KCubeController is importable.
  2. Exposes the standard usphere module protocol (read / test / command).
  3. Persists the last known position to a state file in ipc/ so the GUI
     can display it on boot before homing resets the encoder.

Named preset positions
----------------------
  retrieval  (~5.0 mm)  — stage position for physically accessing the dropper
  dropping   (~6.5 mm)  — aperture aligned with beam; spheres fall into trap
  retraction (~11.0 mm) — coverslip clear of trapping beam; used after trapping

Motor parameter handling
------------------------
Config values of 0.0 mean "keep the device's current setting".
Non-zero values override.  See KCubeController.apply_motion_params().
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the submodule is importable
# ---------------------------------------------------------------------------

_SUBMODULE_DIR = Path(__file__).parent.parent / "resources" / "kcube-motor-controller"
if _SUBMODULE_DIR.exists() and str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

try:
    from kcube_controller import KCubeController
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

MODULE_NAME = "DROPPER_STAGE"
DEVICE_NAME = "Dropper Stage (Z812 / KDC101)"


# ---------------------------------------------------------------------------
# Configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "serial_number",
        "label":   "Serial number",
        "type":    "text",
        "default": "27006288",
    },
    {
        "key":     "retrieval_mm",
        "label":   "Retrieval position (mm)",
        "type":    "float",
        "default": 5.0,
        "tooltip": "Stage position for physically accessing / replacing the dropper module",
    },
    {
        "key":     "dropping_mm",
        "label":   "Dropping position (mm)",
        "type":    "float",
        "default": 6.5,
        "tooltip": "Aperture aligned with trapping beam; spheres can fall into trap",
    },
    {
        "key":     "retraction_mm",
        "label":   "Retraction position (mm)",
        "type":    "float",
        "default": 11.0,
        "tooltip": "Coverslip clear of trapping beam; used after a sphere is trapped",
    },
    {
        "key":     "velocity_mm_s",
        "label":   "Velocity (mm/s)",
        "type":    "float",
        "default": 1.0,
        "tooltip": "0.0 keeps the value already stored in the KDC101",
    },
    {
        "key":     "acceleration_mm_s2",
        "label":   "Acceleration (mm/s²)",
        "type":    "float",
        "default": 1.0,
        "tooltip": "0.0 keeps the value already stored in the KDC101",
    },
    {
        "key":     "jog_step_mm",
        "label":   "Jog step (mm)",
        "type":    "float",
        "default": 0.1,
        "tooltip": "0.0 keeps the value already stored in the KDC101",
    },
    {
        "key":     "backlash_mm",
        "label":   "Backlash (mm)",
        "type":    "float",
        "default": 0.0,
        "tooltip": "0.0 keeps the value already stored in the KDC101",
    },
]


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

KEY_POSITION  = "position_mm"
KEY_IS_HOMED  = "is_homed"
KEY_IS_MOVING = "is_moving"

DEFAULTS: dict = {
    KEY_POSITION:  0.0,
    KEY_IS_HOMED:  0.0,
    KEY_IS_MOVING: 0.0,
}

_PRESETS: dict[str, tuple[str, float]] = {
    "retrieval":  ("retrieval_mm",  5.0),
    "dropping":   ("dropping_mm",   6.5),
    "retraction": ("retraction_mm", 11.0),
}


# ---------------------------------------------------------------------------
# State file — persists last known position across restarts
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).parent.parent / "ipc" / "dropper_stage_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(position_mm: float, note: str = "") -> None:
    _STATE_FILE.parent.mkdir(exist_ok=True)
    state = _load_state()
    state["last_position_mm"] = round(position_mm, 6)
    state["last_updated"]     = datetime.datetime.now().isoformat()
    if note:
        state["last_note"] = note
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_last_position() -> float | None:
    """Return the last recorded position in mm from the state file, or None."""
    return _load_state().get("last_position_mm")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_ctrl(config: dict) -> KCubeController:
    if not CONTROLLER_AVAILABLE:
        raise RuntimeError(
            "KCubeController not found. "
            f"Expected submodule at: {_SUBMODULE_DIR}"
        )
    return KCubeController(config.get("serial_number", CONFIG_FIELDS[0]["default"]))


def _apply_params(ctrl: KCubeController, config: dict) -> None:
    ctrl.apply_motion_params(
        velocity     = float(config.get("velocity_mm_s",      0.0)),
        acceleration = float(config.get("acceleration_mm_s2", 0.0)),
        jog_step     = float(config.get("jog_step_mm",        0.0)),
        backlash     = float(config.get("backlash_mm",        0.0)),
    )


# ---------------------------------------------------------------------------
# Plugin interface — read
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """Connect, read position and status flags, disconnect."""
    ctrl = _make_ctrl(config)
    if not ctrl.connect():
        raise ConnectionError(
            f"Could not connect to dropper stage S/N '{config.get('serial_number')}'."
        )
    try:
        s = ctrl.get_status()
        return {
            KEY_POSITION:  s["position_mm"],
            KEY_IS_HOMED:  1.0 if s["is_homed"]  else 0.0,
            KEY_IS_MOVING: 1.0 if s["is_moving"] else 0.0,
        }
    finally:
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Plugin interface — test
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """Connect, read position and status, report.  Safe to call from a worker thread."""
    try:
        vals = read(config)
        pos       = vals[KEY_POSITION]
        homed_str = "yes" if vals[KEY_IS_HOMED] else "no"
        last      = get_last_position()
        last_str  = f"  |  last saved: {last:.4f} mm" if last is not None else ""
        return True, f"OK — position: {pos:.4f} mm  |  homed: {homed_str}{last_str}"
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Plugin interface — command
# ---------------------------------------------------------------------------

def command(config: dict, **kwargs) -> dict:
    """
    Execute a motor command.  Blocks until the motion completes (or times out).

    Parameters
    ----------
    config : standard config dict
    **kwargs:
        action : str — "home" | "move_to" | "move_to_preset" | "jog"

        For action="move_to":
            position_mm : float

        For action="move_to_preset":
            preset : str — "retrieval" | "dropping" | "retraction"

        For action="jog":
            direction : str — "forward" (default) or "reverse"
            step_mm   : float (optional) — overrides jog_step_mm from config

    Returns
    -------
    dict
        ok          : bool
        message     : str
        position_mm : float
    """
    action = kwargs.get("action", "")

    try:
        ctrl = _make_ctrl(config)
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc), "position_mm": 0.0}

    if not ctrl.connect():
        return {
            "ok": False,
            "message": f"Could not connect to stage S/N '{config.get('serial_number')}'.",
            "position_mm": 0.0,
        }

    try:
        _apply_params(ctrl, config)

        if action == "home":
            pos = ctrl.home()
            _save_state(pos, note="homed")
            return {"ok": True, "message": f"Homed. Position: {pos:.4f} mm", "position_mm": pos}

        elif action == "move_to":
            target = float(kwargs["position_mm"])
            pos = ctrl.move_to(target)
            _save_state(pos, note=f"move_to {target:.4f} mm")
            return {"ok": True, "message": f"Moved to {pos:.4f} mm", "position_mm": pos}

        elif action == "move_to_preset":
            preset = kwargs.get("preset", "")
            if preset not in _PRESETS:
                return {
                    "ok": False,
                    "message": f"Unknown preset {preset!r}. Valid: {list(_PRESETS)}",
                    "position_mm": 0.0,
                }
            cfg_key, fallback = _PRESETS[preset]
            target = float(config.get(cfg_key, fallback))
            pos = ctrl.move_to(target)
            _save_state(pos, note=f"preset:{preset}")
            return {"ok": True, "message": f"At '{preset}': {pos:.4f} mm", "position_mm": pos}

        elif action == "jog":
            direction = kwargs.get("direction", "forward")
            step = float(kwargs.get("step_mm", config.get("jog_step_mm", 0.1)))
            pos = ctrl.jog(direction=direction, step_mm=step)
            _save_state(pos, note=f"jog {direction} {step:.4f} mm")
            return {
                "ok": True,
                "message": f"Jogged {direction} {step:.4f} mm → {pos:.4f} mm",
                "position_mm": pos,
            }

        else:
            return {
                "ok": False,
                "message": f"Unknown action {action!r}. Valid: home, move_to, move_to_preset, jog",
                "position_mm": 0.0,
            }

    except KeyError as exc:
        return {"ok": False, "message": f"Missing parameter: {exc}", "position_mm": 0.0}
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc), "position_mm": 0.0}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "position_mm": 0.0}
    finally:
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys

    cfg = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    if len(_sys.argv) > 1:
        cfg["serial_number"] = _sys.argv[1]

    sn = cfg["serial_number"]
    print(f"Testing {DEVICE_NAME}  S/N {sn} ...")

    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")

    last = get_last_position()
    if last is not None:
        print(f"Last saved position: {last:.4f} mm")

    if not ok:
        _sys.exit(1)

    print("\nAvailable commands:")
    print("  python mod_dropper_stage.py <sn> home")
    print("  python mod_dropper_stage.py <sn> retrieval")
    print("  python mod_dropper_stage.py <sn> dropping")
    print("  python mod_dropper_stage.py <sn> retraction")
    print("  python mod_dropper_stage.py <sn> <position_mm>")

    if len(_sys.argv) > 2:
        arg = _sys.argv[2]
        if arg == "home":
            result = command(cfg, action="home")
        elif arg in _PRESETS:
            result = command(cfg, action="move_to_preset", preset=arg)
        else:
            try:
                result = command(cfg, action="move_to", position_mm=float(arg))
            except ValueError:
                result = {"ok": False, "message": f"Unrecognised argument: {arg}"}
        print(f"{'OK' if result['ok'] else 'FAILED'}: {result['message']}")
