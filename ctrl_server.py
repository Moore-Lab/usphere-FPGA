"""
ctrl_server.py  —  usphere-CTRL ZMQ server

Owns the FPGA session and all hardware modules (TIC, dropper stage, AWG).
Exposes them over ZMQ so that usphere-DAQ, usphere-EXPT scripts, and the
unified GUI can query and command ctrl without touching hardware directly.

Run standalone::

    python ctrl_server.py                    # default ports 5550/5551
    python ctrl_server.py --rep 5550 --pub 5551 --no-gui

The FPGA GUI can still be launched alongside; the server and GUI share the
same FPGAController instance in-process.

ZMQ commands (send to REP port 5550)
-------------------------------------
Built-in (from zmq_base):
    ping                        liveness check
    get_state                   streamed state snapshot
    get_info                    port / module info

FPGA:
    connect                     open FPGA session
    disconnect                  close FPGA session
    read_register   name        read one register → {value}
    write_register  name value  write one register
    read_registers  names=[..]) read a list of registers → {values}
    snapshot                    all registers + metadata → {snapshot}
    write_many      values={..} write multiple registers → {errors}
    change_pars     axis host_params [pid_values]
    ramp            name target step delay_s   (async; returns immediately)
    save_sphere     filepath [host_params]
    load_sphere     filepath    → {errors, host_params}

TIC:
    get_tic                     latest cached TIC readings → {data}
    tic_command     action [speed_pct]  start_pump|stop_pump|set_speed
    set_tic_config  port [baud_rate]    update TIC serial config
    start_tic_poll  [interval_s]        start periodic TIC polling
    stop_tic_poll                       stop TIC polling

Modules:
    list_modules                list available hardware module names
    test_module     name        run module test() → {ok, message}
    read_module     name        run module read() → {data}
    module_command  name [**kwargs]  run module command() → {result}
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import threading
import time
from pathlib import Path

# Ensure this directory is on sys.path when run as a script
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleServer
from fpga_core import FPGAConfig, FPGAController

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardware module registry (mirrors ctrl's modules/ plugin system)
# ---------------------------------------------------------------------------

_MODULE_NAMES: list[str] = [
    "modules.mod_edwards_tic",
    "modules.mod_dropper_stage",
    "modules.mod_keysight_awg",
]

_MODULES: dict[str, object] = {}   # MODULE_NAME → module object


def _load_modules() -> None:
    for mod_path in _MODULE_NAMES:
        try:
            mod = importlib.import_module(mod_path)
            _MODULES[mod.MODULE_NAME] = mod
        except Exception as exc:
            log.warning("Could not load module %s: %s", mod_path, exc)


_load_modules()


# ---------------------------------------------------------------------------
# CtrlServer
# ---------------------------------------------------------------------------

class CtrlServer(ModuleServer):
    """
    ZMQ server wrapping FPGAController + hardware modules.

    TIC readings are cached by a background poll thread so that
    get_tic commands return immediately without blocking on serial I/O.
    """

    def __init__(
        self,
        fpga_controller: FPGAController,
        rep_port: int = 5550,
        pub_port: int = 5551,
        publish_interval_s: float = 0.2,
    ) -> None:
        super().__init__(
            module_name="ctrl",
            rep_port=rep_port,
            pub_port=pub_port,
            publish_interval_s=publish_interval_s,
        )
        self.fpga = fpga_controller

        # TIC state — updated by background poll thread
        self._tic_config: dict = {"port": "COM3", "baud_rate": "9600"}
        self._tic_cache: dict = {}
        self._tic_lock = threading.Lock()
        self._tic_poll_stop = threading.Event()
        self._tic_poll_thread: threading.Thread | None = None
        self._tic_poll_interval_s: float = 2.0

        # Latest FPGA register snapshot — updated by FPGAController monitor
        self._register_cache: dict[str, float] = {}
        self._reg_lock = threading.Lock()

        # Wire FPGA monitor callback to update our cache
        self.fpga._on_registers_updated = self._on_registers_updated

    # ------------------------------------------------------------------
    # FPGA monitor callback
    # ------------------------------------------------------------------

    def _on_registers_updated(self, values: dict[str, float]) -> None:
        with self._reg_lock:
            self._register_cache = dict(values)

    # ------------------------------------------------------------------
    # get_state — streamed by PUB loop
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        with self._reg_lock:
            regs = dict(self._register_cache)
        with self._tic_lock:
            tic = dict(self._tic_cache)
        return {
            "connected":  self.fpga.is_connected,
            "simulated":  self.fpga.is_simulated,
            "registers":  regs,
            "tic":        tic,
        }

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str, args: dict) -> dict:
        try:
            return self._dispatch_ctrl(cmd, args)
        except Exception as exc:
            log.exception("Command %r raised", cmd)
            return {"status": "error", "message": str(exc)}

    def _dispatch_ctrl(self, cmd: str, args: dict) -> dict:

        # ---- FPGA connection ----
        if cmd == "connect":
            self.fpga.connect()
            if not self.fpga._monitor_thread or not self.fpga._monitor_thread.is_alive():
                self.fpga.start_monitor()
            return {"status": "ok"}

        if cmd == "disconnect":
            self.fpga.disconnect()
            return {"status": "ok"}

        # ---- FPGA register I/O ----
        if cmd == "read_register":
            name = args["name"]
            value = self.fpga.read_register(name)
            return {"status": "ok", "data": {"name": name, "value": value}}

        if cmd == "write_register":
            self.fpga.write_register(args["name"], float(args["value"]))
            return {"status": "ok"}

        if cmd == "read_registers":
            names = args["names"]
            values = self.fpga.read_registers(names)
            return {"status": "ok", "data": values}

        if cmd == "snapshot":
            return {"status": "ok", "data": self.fpga.snapshot()}

        if cmd == "write_many":
            errors = self.fpga.write_many(args["values"])
            return {"status": "ok", "data": {"errors": errors}}

        if cmd == "change_pars":
            errors = self.fpga.change_pars(
                axis=args["axis"],
                host_params=args["host_params"],
                pid_values=args.get("pid_values"),
            )
            return {"status": "ok", "data": {"errors": errors}}

        if cmd == "ramp":
            self.fpga.ramp_register(
                name=args["name"],
                target=float(args["target"]),
                step=float(args["step"]),
                delay_s=float(args["delay_s"]),
            )
            return {"status": "ok", "data": {"message": "ramp started"}}

        if cmd == "save_sphere":
            fp = self.fpga.save_sphere(
                filepath=args["filepath"],
                host_params=args.get("host_params"),
            )
            return {"status": "ok", "data": {"filepath": str(fp)}}

        if cmd == "load_sphere":
            errors, host_params = self.fpga.load_sphere(args["filepath"])
            return {"status": "ok", "data": {"errors": errors, "host_params": host_params}}

        if cmd == "save_snapshot":
            fp = self.fpga.save_snapshot(args["filepath"])
            return {"status": "ok", "data": {"filepath": str(fp)}}

        if cmd == "load_snapshot":
            errors = self.fpga.load_snapshot(args["filepath"])
            return {"status": "ok", "data": {"errors": errors}}

        # ---- TIC ----
        if cmd == "get_tic":
            with self._tic_lock:
                return {"status": "ok", "data": dict(self._tic_cache)}

        if cmd == "set_tic_config":
            with self._tic_lock:
                if "port" in args:
                    self._tic_config["port"] = args["port"]
                if "baud_rate" in args:
                    self._tic_config["baud_rate"] = str(args["baud_rate"])
            return {"status": "ok"}

        if cmd == "start_tic_poll":
            interval = float(args.get("interval_s", self._tic_poll_interval_s))
            self._start_tic_poll(interval)
            return {"status": "ok"}

        if cmd == "stop_tic_poll":
            self._stop_tic_poll()
            return {"status": "ok"}

        if cmd == "tic_command":
            mod = _MODULES.get("EDWARDS_TIC")
            if mod is None:
                return {"status": "error", "message": "EDWARDS_TIC module not loaded"}
            with self._tic_lock:
                cfg = dict(self._tic_config)
            result = mod.command(cfg, **args)
            return {"status": "ok", "data": result}

        # ---- Hardware modules ----
        if cmd == "list_modules":
            return {"status": "ok", "data": list(_MODULES.keys())}

        if cmd == "test_module":
            name = args["name"]
            mod = _MODULES.get(name)
            if mod is None:
                return {"status": "error", "message": f"Module {name!r} not loaded"}
            config = args.get("config", {})
            ok, msg = mod.test(config)
            return {"status": "ok", "data": {"ok": ok, "message": msg}}

        if cmd == "read_module":
            name = args["name"]
            mod = _MODULES.get(name)
            if mod is None:
                return {"status": "error", "message": f"Module {name!r} not loaded"}
            config = args.get("config", {})
            data = mod.read(config)
            return {"status": "ok", "data": data}

        if cmd == "module_command":
            name = args.pop("name")
            mod = _MODULES.get(name)
            if mod is None:
                return {"status": "error", "message": f"Module {name!r} not loaded"}
            config = args.pop("config", {})
            result = mod.command(config, **args)
            return {"status": "ok", "data": result}

        return {"status": "error", "message": f"unknown command: {cmd!r}"}

    # ------------------------------------------------------------------
    # TIC background poll
    # ------------------------------------------------------------------

    def _start_tic_poll(self, interval_s: float = 2.0) -> None:
        self._stop_tic_poll()
        self._tic_poll_interval_s = interval_s
        self._tic_poll_stop.clear()
        self._tic_poll_thread = threading.Thread(
            target=self._tic_poll_loop, daemon=True, name="ctrl-tic-poll"
        )
        self._tic_poll_thread.start()
        log.info("TIC poll started (interval=%.1fs)", interval_s)

    def _stop_tic_poll(self) -> None:
        self._tic_poll_stop.set()
        if self._tic_poll_thread is not None:
            self._tic_poll_thread.join(timeout=2.0)
            self._tic_poll_thread = None

    def _tic_poll_loop(self) -> None:
        mod = _MODULES.get("EDWARDS_TIC")
        if mod is None:
            log.warning("TIC poll started but EDWARDS_TIC module not loaded")
            return
        while not self._tic_poll_stop.is_set():
            try:
                with self._tic_lock:
                    cfg = dict(self._tic_config)
                data = mod.read(cfg)
                data["ts"] = time.time()
                with self._tic_lock:
                    self._tic_cache = data
            except Exception as exc:
                with self._tic_lock:
                    self._tic_cache = {"error": str(exc), "ts": time.time()}
            self._tic_poll_stop.wait(self._tic_poll_interval_s)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="usphere-CTRL ZMQ server")
    p.add_argument("--rep",    type=int, default=5550, help="REP port (default 5550)")
    p.add_argument("--pub",    type=int, default=5551, help="PUB port (default 5551)")
    p.add_argument("--no-gui", action="store_true",    help="headless mode (no Qt GUI)")
    p.add_argument("--connect",action="store_true",    help="connect to FPGA on startup")
    p.add_argument("--tic-port", default=None,         help="Edwards TIC serial port (e.g. COM3)")
    p.add_argument("--tic-poll", type=float, default=None,
                   help="start TIC polling with this interval in seconds")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    args = _parse_args()

    fpga = FPGAController()
    server = CtrlServer(fpga, rep_port=args.rep, pub_port=args.pub)
    server.start()
    log.info("ctrl server listening  REP=tcp://*:%d  PUB=tcp://*:%d", args.rep, args.pub)

    if args.connect:
        fpga.connect()
        fpga.start_monitor()
        log.info("FPGA connected")

    if args.tic_port:
        server._tic_config["port"] = args.tic_port

    if args.tic_poll is not None:
        server._start_tic_poll(args.tic_poll)
        log.info("TIC polling started (%.1fs interval)", args.tic_poll)

    if args.no_gui:
        log.info("Running headless — Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            fpga.disconnect()
    else:
        # Launch the FPGA GUI alongside the server.
        # The GUI and server share the same FPGAController instance.
        try:
            from PyQt5.QtWidgets import QApplication
            import fpga_gui as gui_mod
        except ImportError as exc:
            log.error("Cannot import GUI (%s) — rerun with --no-gui", exc)
            sys.exit(1)

        app = QApplication(sys.argv)
        window = gui_mod.FPGAWindow(controller=fpga)
        window.show()
        try:
            sys.exit(app.exec_())
        finally:
            server.stop()
            fpga.disconnect()


if __name__ == "__main__":
    main()
