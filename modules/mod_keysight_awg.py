"""
modules/mod_keysight_awg.py

Hardware module for the Keysight 33500B Arbitrary Waveform Generator.

Used during Stage 5 (dropper shaking): drives a continuous frequency sweep
through the dropper piezo to shake microspheres off the dropper tip and into
the trapping beam.

Driver location
---------------
The Keysight 33500B driver lives in the git submodule at:
  resources/KEYSIGHT33500B/
This module inserts that path into sys.path at load time so that
``ks33500b_controller.KS33500BController`` can be imported without
a package install.

Module protocol
---------------
  MODULE_NAME   "KEYSIGHT_AWG"
  DEVICE_NAME   "Keysight 33500B AWG"
  CONFIG_FIELDS VISA resource string, channel, sweep params, amplitude
  DEFAULTS      is_connected, amplitude_vpp, frequency_hz, output_on
  read(config)  connect, query per-channel status, disconnect
  test(config)  connect, report IDN + channel state
  command(config, action, **kwargs)
                setup_sweep | output_on | output_off | set_amplitude | reset

Default sweep parameters (from trapping-protocol Stage 5 LabVIEW VI)
---------------------------------------------------------------------
  start_freq : 100 kHz
  stop_freq  : 700 kHz
  sweep_time : 0.1 s
  amplitude  : 0.1 Vpp  (starting value; ramped upward during shaking)

VISA address note
-----------------
The default resource string matches the hostname observed in the LabVIEW VI
(K-33511B-01756).  For USB connections use a string like
"USB0::0x0957::0x2C07::MY57801234::INSTR".  Pass "auto" to let the driver
discover the first available 33500B on the network/USB.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the submodule driver is importable
# ---------------------------------------------------------------------------

_SUBMODULE_DIR = Path(__file__).parent.parent / "resources" / "KEYSIGHT33500B"
if _SUBMODULE_DIR.exists() and str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

try:
    from ks33500b_controller import KS33500BController
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

MODULE_NAME = "KEYSIGHT_AWG"
DEVICE_NAME = "Keysight 33500B AWG"


# ---------------------------------------------------------------------------
# Configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "resource_name",
        "label":   "VISA resource",
        "type":    "text",
        "default": "TCPIP0::K-33511B-01756::inst0::INSTR",
        "tooltip": (
            "VISA address string. "
            "Examples: 'TCPIP0::192.168.1.100::inst0::INSTR' (LAN), "
            "'USB0::0x0957::0x2C07::MY57801234::INSTR' (USB). "
            "Use 'auto' to auto-discover the first available 33500B."
        ),
    },
    {
        "key":     "channel",
        "label":   "Channel",
        "type":    "int",
        "default": 1,
        "tooltip": "Output channel (1 or 2).",
    },
    {
        "key":     "start_freq_hz",
        "label":   "Start frequency (Hz)",
        "type":    "float",
        "default": 100_000.0,
        "tooltip": "Lower bound of the frequency sweep in Hz.",
    },
    {
        "key":     "stop_freq_hz",
        "label":   "Stop frequency (Hz)",
        "type":    "float",
        "default": 700_000.0,
        "tooltip": "Upper bound of the frequency sweep in Hz.",
    },
    {
        "key":     "sweep_time_s",
        "label":   "Sweep time (s)",
        "type":    "float",
        "default": 0.1,
        "tooltip": "Time for a single sweep from start_freq to stop_freq.",
    },
    {
        "key":     "amplitude_vpp",
        "label":   "Amplitude (Vpp)",
        "type":    "float",
        "default": 0.1,
        "tooltip": "Initial peak-to-peak carrier amplitude. Ramped up during shaking.",
    },
]


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

KEY_IS_CONNECTED = "is_connected"
KEY_AMPLITUDE    = "amplitude_vpp"
KEY_FREQUENCY    = "frequency_hz"
KEY_OUTPUT_ON    = "output_on"

DEFAULTS: dict = {
    KEY_IS_CONNECTED: 0.0,
    KEY_AMPLITUDE:    0.0,
    KEY_FREQUENCY:    0.0,
    KEY_OUTPUT_ON:    0.0,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_controller(config: dict) -> KS33500BController:
    """Return an unconnected KS33500BController configured from *config*."""
    resource = config.get("resource_name", CONFIG_FIELDS[0]["default"]).strip()
    if resource.lower() == "auto":
        resource = None
    return KS33500BController(resource_name=resource, timeout=5000)


def _connect(config: dict) -> KS33500BController:
    """
    Open a connection to the AWG and return the controller.

    Raises
    ------
    RuntimeError   if the driver submodule is not present
    ConnectionError if the device cannot be reached
    """
    if not CONTROLLER_AVAILABLE:
        raise RuntimeError(
            "Keysight 33500B driver not found. "
            f"Expected submodule at: {_SUBMODULE_DIR}"
        )

    ctrl = _make_controller(config)
    resource = config.get("resource_name", "").strip()

    if resource.lower() == "auto" or not resource:
        ok = ctrl.auto_connect()
    else:
        ok = ctrl.connect(resource)

    if not ok:
        raise ConnectionError(
            f"Could not connect to Keysight 33500B at '{resource}'. "
            "Check VISA resource string and network/USB connection."
        )
    return ctrl


# ---------------------------------------------------------------------------
# Plugin interface — read
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Connect to the AWG, query channel status, disconnect.

    Parameters
    ----------
    config : dict with at minimum "resource_name" and "channel"

    Returns
    -------
    dict
        is_connected : float — 1.0 if query succeeded
        amplitude_vpp: float — measured carrier amplitude in Vpp
        frequency_hz : float — measured carrier frequency in Hz
        output_on    : float — 1.0 if channel output is enabled

    Raises
    ------
    RuntimeError    if the driver is not available
    ConnectionError if the device cannot be reached
    Exception       on any VISA communication error
    """
    ch   = int(config.get("channel", 1))
    ctrl = _connect(config)
    try:
        amp  = ctrl.get_amplitude(ch) or 0.0
        freq = ctrl.get_frequency(ch) or 0.0
        on   = 1.0 if ctrl.is_output_on(ch) else 0.0
        return {
            KEY_IS_CONNECTED: 1.0,
            KEY_AMPLITUDE:    float(amp),
            KEY_FREQUENCY:    float(freq),
            KEY_OUTPUT_ON:    on,
        }
    finally:
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Plugin interface — test
# ---------------------------------------------------------------------------

def test(config: dict) -> tuple[bool, str]:
    """
    Connect, report IDN + channel state.  Safe to call from a worker thread.
    """
    try:
        ch   = int(config.get("channel", 1))
        ctrl = _connect(config)
        try:
            idn  = ctrl.idn or "?"
            amp  = ctrl.get_amplitude(ch)
            freq = ctrl.get_frequency(ch)
            on   = ctrl.is_output_on(ch)
            amp_str  = f"{amp:.4f} Vpp" if amp is not None else "?"
            freq_str = f"{freq/1e3:.1f} kHz" if freq is not None else "?"
            out_str  = "ON" if on else "OFF"
            return True, (
                f"OK — {idn}  |  ch{ch}: {amp_str} @ {freq_str}, output {out_str}"
            )
        finally:
            ctrl.disconnect()
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Plugin interface — command
# ---------------------------------------------------------------------------

def command(config: dict, **kwargs) -> dict:
    """
    Execute an AWG command.  Opens a fresh connection, runs the command,
    and disconnects.

    Parameters
    ----------
    config : standard config dict
    **kwargs:
        action : str — one of "setup_sweep", "output_on", "output_off",
                        "set_amplitude", "reset"

        For action="setup_sweep":
            amplitude_vpp : float (optional, overrides config value)

        For action="set_amplitude":
            amplitude_vpp : float — new amplitude in Vpp

    Returns
    -------
    dict
        ok           : bool
        message      : str
        amplitude_vpp: float (current amplitude after command; 0.0 if unavailable)
        output_on    : bool
    """
    action  = kwargs.get("action", "")
    ch      = int(config.get("channel", 1))

    try:
        ctrl = _connect(config)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "amplitude_vpp": 0.0, "output_on": False}

    try:
        # ---- setup_sweep ----
        if action == "setup_sweep":
            amp = float(kwargs.get("amplitude_vpp", config.get("amplitude_vpp", 0.1)))
            ok  = ctrl.setup_sweep(
                channel    = ch,
                start_freq = float(config.get("start_freq_hz", 100e3)),
                stop_freq  = float(config.get("stop_freq_hz",  700e3)),
                sweep_time = float(config.get("sweep_time_s",  0.1)),
                waveform   = "sine",
                amplitude  = amp,
                spacing    = "linear",
                trigger    = "immediate",
            )
            if not ok:
                return {
                    "ok": False,
                    "message": "setup_sweep command failed (VISA error)",
                    "amplitude_vpp": 0.0,
                    "output_on": False,
                }
            return {
                "ok": True,
                "message": (
                    f"Sweep configured: {config.get('start_freq_hz',100e3)/1e3:.0f}–"
                    f"{config.get('stop_freq_hz',700e3)/1e3:.0f} kHz, "
                    f"{config.get('sweep_time_s',0.1):.2f} s, {amp:.4f} Vpp"
                ),
                "amplitude_vpp": amp,
                "output_on": False,
            }

        # ---- output_on ----
        elif action == "output_on":
            ok = ctrl.output_on(ch)
            return {
                "ok": ok,
                "message": f"Output ch{ch} {'ON' if ok else 'failed to enable'}",
                "amplitude_vpp": ctrl.get_amplitude(ch) or 0.0,
                "output_on": ok,
            }

        # ---- output_off ----
        elif action == "output_off":
            ok = ctrl.output_off(ch)
            return {
                "ok": ok,
                "message": f"Output ch{ch} {'OFF' if ok else 'failed to disable'}",
                "amplitude_vpp": ctrl.get_amplitude(ch) or 0.0,
                "output_on": False,
            }

        # ---- set_amplitude ----
        elif action == "set_amplitude":
            amp = float(kwargs["amplitude_vpp"])
            ok  = ctrl.set_amplitude(ch, amp)
            return {
                "ok": ok,
                "message": f"Amplitude ch{ch} set to {amp:.4f} Vpp" if ok else "set_amplitude failed",
                "amplitude_vpp": amp if ok else 0.0,
                "output_on": ctrl.is_output_on(ch),
            }

        # ---- reset ----
        elif action == "reset":
            ok = ctrl.reset()
            return {
                "ok": ok,
                "message": "Instrument reset (*RST)" if ok else "Reset failed",
                "amplitude_vpp": 0.0,
                "output_on": False,
            }

        else:
            return {
                "ok": False,
                "message": (
                    f"Unknown action {action!r}. "
                    "Valid actions: setup_sweep, output_on, output_off, "
                    "set_amplitude, reset"
                ),
                "amplitude_vpp": 0.0,
                "output_on": False,
            }

    except KeyError as exc:
        return {"ok": False, "message": f"Missing parameter: {exc}", "amplitude_vpp": 0.0, "output_on": False}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "amplitude_vpp": 0.0, "output_on": False}
    finally:
        ctrl.disconnect()


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys

    cfg = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    if len(_sys.argv) > 1:
        cfg["resource_name"] = _sys.argv[1]

    print(f"Testing {DEVICE_NAME}  resource='{cfg['resource_name']}' ...")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")

    if not ok:
        _sys.exit(1)

    print("\nAvailable commands:")
    print("  python mod_keysight_awg.py <resource> setup")
    print("  python mod_keysight_awg.py <resource> on")
    print("  python mod_keysight_awg.py <resource> off")
    print("  python mod_keysight_awg.py <resource> reset")

    if len(_sys.argv) > 2:
        action_arg = _sys.argv[2]
        action_map = {
            "setup": ("setup_sweep", {}),
            "on":    ("output_on",   {}),
            "off":   ("output_off",  {}),
            "reset": ("reset",       {}),
        }
        if action_arg in action_map:
            act, kw = action_map[action_arg]
            result = command(cfg, action=act, **kw)
            print(f"{'OK' if result['ok'] else 'FAILED'}: {result['message']}")
        else:
            print(f"Unrecognised argument: {action_arg}")
