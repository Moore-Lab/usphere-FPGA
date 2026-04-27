"""
modules/mod_dropper_stage.py

Hardware module for the dropper translation stage:
  Thorlabs Z812 linear actuator + KDC101 KCube DC Servo Motor Controller.

Communicates via USB using the Thorlabs Kinesis SDK, wrapped by pylablib.

Dependencies
------------
  pip install pylablib
  Kinesis software must be installed (provides the backend DLLs):
  https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=Motion_Control

  NOTE: pylablib's scale="Z812" returns positions in METERS (34304E3 steps/m).
  We pass scale=34304 (steps/mm) explicitly so all position values are in mm.

Module protocol
---------------
  MODULE_NAME   "DROPPER_STAGE"
  DEVICE_NAME   "Dropper Stage (Z812 / KDC101)"
  CONFIG_FIELDS serial number, three named preset positions, motion params
  DEFAULTS      position_mm, is_homed, is_moving
  read(config)  return current position and status flags
  test(config)  connect, read, report
  command(config, action, **kwargs)
                home | move_to | move_to_preset | jog

Named preset positions
----------------------
  retrieval  (~5.0 mm)  — stage position for physically accessing the dropper
  dropping   (~6.5 mm)  — aperture aligned with beam; spheres fall into trap
  retraction (~11.0 mm) — coverslip clear of trapping beam; used after trapping

These are stored in config (editable in the GUI) and are stable across hardware
swaps for a given optical alignment.  If a different dropper module is installed
the "dropping" position may shift by ~1 mm and should be re-dialled in.

State file (dropper_stage_state.json, same directory as this module)
Records last_position_mm after each commanded move so the GUI can display the
last known position on boot — before homing resets the encoder.

Motor parameter handling
------------------------
On connect, the KDC101 already holds its last-configured values for velocity,
acceleration, jog step size, and backlash (stored in the controller's
non-volatile memory by the Kinesis GUI or a previous session).  Config values
of 0.0 mean "keep the device's current setting".  Non-zero values override.
This means the first time the controller is used you can leave everything at
0.0 and get sensible defaults; only change what you actually want to tune.

Typical operating values: velocity 1 mm/s, acceleration 1 mm/s².
The Z812 travel range is 0–12 mm.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import time
from pathlib import Path

try:
    from pylablib.devices import Thorlabs
    PYLABLIB_AVAILABLE = True
except ImportError:
    PYLABLIB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

MODULE_NAME = "DROPPER_STAGE"
DEVICE_NAME = "Dropper Stage (Z812 / KDC101)"

# Physical travel range of the Z812 (used for input validation)
_Z812_MIN_MM = 0.0
_Z812_MAX_MM = 12.0


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
    # --- Named preset positions ---
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
    # --- Motion parameters (0.0 = keep device default) ---
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

# Map preset name → config key → fallback default
_PRESETS: dict[str, tuple[str, float]] = {
    "retrieval":  ("retrieval_mm",  5.0),
    "dropping":   ("dropping_mm",   6.5),
    "retraction": ("retraction_mm", 11.0),
}


# ---------------------------------------------------------------------------
# State file — persists last known position across restarts
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).parent / "dropper_stage_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(position_mm: float, note: str = "") -> None:
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

@contextlib.contextmanager
def _open(serial_number: str):
    """Open a KinesisMotor for the Z812 and yield it after a brief settle.

    The KDC101 communicates over USB-serial.  Rapid open/close cycles can
    leave stale bytes in the OS buffer that are then mis-read as the start
    of the next response message, producing a "message sync error".  The
    100 ms sleep after opening gives the driver time to drain any residual
    data before the first command is sent.
    """
    with Thorlabs.KinesisMotor(serial_number, scale=34304) as motor:
        time.sleep(0.5)
        yield motor


def _apply_motion_params(stage, config: dict) -> None:
    """
    Push velocity / acceleration / jog step / backlash to the controller.
    Values of 0.0 in config are skipped, preserving whatever is in the device.
    """
    vel  = float(config.get("velocity_mm_s",      0.0))
    acc  = float(config.get("acceleration_mm_s2", 0.0))
    jog  = float(config.get("jog_step_mm",        0.0))
    blsh = float(config.get("backlash_mm",        0.0))

    # pylablib passes None through as "keep current value"
    if vel > 0 or acc > 0:
        stage.setup_velocity(
            max_velocity=vel if vel > 0 else None,
            acceleration=acc if acc > 0 else None,
        )

    if jog > 0:
        stage.setup_jog(step_size=jog)

    if blsh > 0:
        stage.set_backlash(blsh)


def _validate_position(position_mm: float) -> None:
    if not (_Z812_MIN_MM <= position_mm <= _Z812_MAX_MM):
        raise ValueError(
            f"Position {position_mm:.4f} mm is outside Z812 travel range "
            f"({_Z812_MIN_MM}–{_Z812_MAX_MM} mm)"
        )


# ---------------------------------------------------------------------------
# Plugin interface — read
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Connect to the KDC101, read position and status flags, then disconnect.

    Parameters
    ----------
    config : dict with at minimum "serial_number"

    Returns
    -------
    dict
        position_mm : float — current position in mm
        is_homed    : float — 1.0 if the stage has been homed, 0.0 otherwise
        is_moving   : float — 1.0 if a move is in progress

    Raises
    ------
    RuntimeError    if pylablib is not installed
    Exception       on any Kinesis communication error
    """
    if not PYLABLIB_AVAILABLE:
        raise RuntimeError("pylablib not installed — run: pip install pylablib")

    sn = config.get("serial_number", CONFIG_FIELDS[0]["default"])

    with _open(sn) as stage:
        position  = stage.get_position()
        status    = stage.get_status()
        is_homed  = 1.0 if "homed" in status else 0.0
        is_moving = 1.0 if stage.is_moving() else 0.0

    return {
        KEY_POSITION:  float(position),
        KEY_IS_HOMED:  is_homed,
        KEY_IS_MOVING: is_moving,
    }


# ---------------------------------------------------------------------------
# Plugin interface — test
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """
    Connect, read position and status, report.  Safe to call from a worker thread.
    """
    try:
        vals = read(config)
        pos       = vals[KEY_POSITION]
        homed_str = "yes" if vals[KEY_IS_HOMED] else "no"
        last      = get_last_position()
        last_str  = f"  |  last saved: {last:.4f} mm" if last is not None else ""
        return True, (
            f"OK — position: {pos:.4f} mm  |  homed: {homed_str}{last_str}"
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
    Execute a motor command.  Blocks until the motion completes (or times out).

    Parameters
    ----------
    config : standard config dict (serial_number, preset positions, motion params)
    **kwargs:
        action : str — one of "home", "move_to", "move_to_preset", "jog"

        For action="move_to":
            position_mm : float — target absolute position in mm

        For action="move_to_preset":
            preset : str — "retrieval", "dropping", or "retraction"

        For action="jog":
            direction : str — "forward" (default) or "reverse"
            step_mm   : float (optional) — override jog_step_mm from config

    Returns
    -------
    dict
        ok          : bool
        message     : str
        position_mm : float (position after move; 0.0 if unavailable)
    """
    if not PYLABLIB_AVAILABLE:
        return {
            "ok": False,
            "message": "pylablib not installed — run: pip install pylablib",
            "position_mm": 0.0,
        }

    action = kwargs.get("action", "")
    sn     = config.get("serial_number", CONFIG_FIELDS[0]["default"])

    try:
        with _open(sn) as stage:
            _apply_motion_params(stage, config)

            # ---- home ----
            if action == "home":
                stage.home(sync=True, timeout=60.0)
                pos = stage.get_position()
                _save_state(pos, note="homed")
                return {
                    "ok": True,
                    "message": f"Homed successfully. Position: {pos:.4f} mm",
                    "position_mm": float(pos),
                }

            # ---- move to absolute position ----
            elif action == "move_to":
                target = float(kwargs["position_mm"])
                _validate_position(target)
                stage.move_to(target);stage.wait_move(timeout=60.0)
                pos = stage.get_position()
                _save_state(pos, note=f"move_to {target:.4f} mm")
                return {
                    "ok": True,
                    "message": f"Moved to {pos:.4f} mm",
                    "position_mm": float(pos),
                }

            # ---- move to named preset ----
            elif action == "move_to_preset":
                preset = kwargs.get("preset", "")
                if preset not in _PRESETS:
                    return {
                        "ok": False,
                        "message": (
                            f"Unknown preset {preset!r}. "
                            f"Valid presets: {list(_PRESETS)}"
                        ),
                        "position_mm": 0.0,
                    }
                cfg_key, fallback = _PRESETS[preset]
                target = float(config.get(cfg_key, fallback))
                _validate_position(target)
                stage.move_to(target);stage.wait_move(timeout=60.0)
                pos = stage.get_position()
                _save_state(pos, note=f"preset:{preset}")
                return {
                    "ok": True,
                    "message": f"At '{preset}': {pos:.4f} mm",
                    "position_mm": float(pos),
                }

            # ---- jog ----
            elif action == "jog":
                direction = kwargs.get("direction", "forward")
                # Allow per-call step override; fall back to config
                step = float(kwargs.get("step_mm", config.get("jog_step_mm", 0.1)))
                if direction not in ("forward", "reverse"):
                    return {
                        "ok": False,
                        "message": f"Unknown jog direction {direction!r}. "
                                   "Use 'forward' or 'reverse'.",
                        "position_mm": 0.0,
                    }
                signed_step = step if direction == "forward" else -step
                current = stage.get_position()
                target  = current + signed_step
                _validate_position(target)
                stage.move_by(signed_step);stage.wait_move(timeout=60.0)
                pos = stage.get_position()
                _save_state(pos, note=f"jog {signed_step:+.4f} mm")
                return {
                    "ok": True,
                    "message": f"Jogged {signed_step:+.4f} mm → {pos:.4f} mm",
                    "position_mm": float(pos),
                }

            else:
                return {
                    "ok": False,
                    "message": (
                        f"Unknown action {action!r}. "
                        "Valid actions: home, move_to, move_to_preset, jog"
                    ),
                    "position_mm": 0.0,
                }

    except KeyError as exc:
        return {"ok": False, "message": f"Missing parameter: {exc}", "position_mm": 0.0}
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "position_mm": 0.0}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "position_mm": 0.0}


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    cfg = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    if len(sys.argv) > 1:
        cfg["serial_number"] = sys.argv[1]

    sn = cfg["serial_number"]
    print(f"Testing {DEVICE_NAME}  S/N {sn} ...")

    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")

    last = get_last_position()
    if last is not None:
        print(f"Last saved position: {last:.4f} mm")

    if not ok:
        sys.exit(1)

    print("\nAvailable commands:")
    print("  python mod_dropper_stage.py <sn> home")
    print("  python mod_dropper_stage.py <sn> retrieval")
    print("  python mod_dropper_stage.py <sn> dropping")
    print("  python mod_dropper_stage.py <sn> retraction")

    if len(sys.argv) > 2:
        action_arg = sys.argv[2]
        if action_arg == "home":
            result = command(cfg, action="home")
        elif action_arg in _PRESETS:
            result = command(cfg, action="move_to_preset", preset=action_arg)
        else:
            try:
                target = float(action_arg)
                result = command(cfg, action="move_to", position_mm=target)
            except ValueError:
                result = {"ok": False, "message": f"Unrecognised argument: {action_arg}"}
        print(f"{'OK' if result['ok'] else 'FAILED'}: {result['message']}")
