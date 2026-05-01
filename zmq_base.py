"""
zmq_base.py  —  usphere module ZMQ contract

This file is IDENTICAL in usphere-CTRL, usphere-DAQ, usphere-Q, and
usphere-EXPT. When updating, copy to all four locations.

Protocol
--------
Each module runs two ZMQ sockets:

    REP socket (rep_port)  — synchronous command / query
    PUB socket (pub_port)  — async live-data stream

Request (JSON sent to REP):
    {"cmd": "<command>", "args": {...}}

Reply (JSON received from REP):
    {"status": "ok",    "data":    {...}}
    {"status": "error", "message": "..."}

Published (JSON received from PUB):
    {"module": "<name>", "ts": <float>, "data": {...}}

Built-in commands handled automatically by ModuleServer:
    ping        — liveness check; returns ts
    get_state   — calls get_state() on the server
    get_info    — module name, ports, publish interval

Module-specific commands go through handle_command().
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable


# ---------------------------------------------------------------------------
# Server base
# ---------------------------------------------------------------------------

class ModuleServer:
    """
    Base ZMQ server for a usphere module.

    Subclass and override:
        handle_command(cmd, args) -> dict   module-specific commands
        get_state() -> dict                 streamed by the PUB loop

    The REP and PUB loops run in daemon threads started by start().
    stop() signals them to exit; the server can then be garbage-collected.

    Example::

        class CtrlServer(ModuleServer):
            def handle_command(self, cmd, args):
                if cmd == "set_gain":
                    self.controller.set_gain(args["axis"], args["value"])
                    return {"status": "ok"}
                return super().handle_command(cmd, args)

            def get_state(self):
                return self.controller.get_status()

        srv = CtrlServer("ctrl", rep_port=5550, pub_port=5551)
        srv.start()
    """

    def __init__(
        self,
        module_name: str,
        rep_port: int,
        pub_port: int,
        publish_interval_s: float = 0.1,
    ) -> None:
        self.module_name = module_name
        self.rep_port = rep_port
        self.pub_port = pub_port
        self.publish_interval_s = publish_interval_s
        self._running = False
        self._rep_thread: threading.Thread | None = None
        self._pub_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Override in subclass
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str, args: dict) -> dict:
        """Handle a module-specific command. Return a reply dict."""
        return {"status": "error", "message": f"unknown command: {cmd!r}"}

    def get_state(self) -> dict:
        """Return the current state dict. Called by the PUB loop."""
        return {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start REP and PUB threads (non-blocking)."""
        import zmq  # deferred: module loads even without zmq installed
        zmq.Context.instance()  # warm up the context
        self._running = True
        self._rep_thread = threading.Thread(
            target=self._rep_loop, daemon=True,
            name=f"{self.module_name}-rep",
        )
        self._pub_thread = threading.Thread(
            target=self._pub_loop, daemon=True,
            name=f"{self.module_name}-pub",
        )
        self._rep_thread.start()
        self._pub_thread.start()

    def stop(self) -> None:
        """Signal both threads to exit."""
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    def _rep_loop(self) -> None:
        import zmq
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REP)
        sock.setsockopt(zmq.RCVTIMEO, 200)   # ms; lets us check _running
        sock.bind(f"tcp://*:{self.rep_port}")
        try:
            while self._running:
                try:
                    raw = sock.recv()
                except zmq.Again:
                    continue
                try:
                    msg = json.loads(raw)
                    reply = self._dispatch(msg.get("cmd", ""), msg.get("args", {}))
                except Exception as exc:
                    reply = {"status": "error", "message": str(exc)}
                sock.send(json.dumps(reply).encode())
        finally:
            sock.close()

    def _pub_loop(self) -> None:
        import zmq
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.PUB)
        sock.bind(f"tcp://*:{self.pub_port}")
        try:
            while self._running:
                try:
                    state = self.get_state()
                except Exception:
                    state = {}
                payload = json.dumps({
                    "module": self.module_name,
                    "ts":     time.time(),
                    "data":   state,
                })
                sock.send(payload.encode())
                time.sleep(self.publish_interval_s)
        finally:
            sock.close()

    def _dispatch(self, cmd: str, args: dict) -> dict:
        if cmd == "ping":
            return {"status": "ok", "data": {
                "module": self.module_name,
                "ts":     time.time(),
            }}
        if cmd == "get_state":
            try:
                return {"status": "ok", "data": self.get_state()}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if cmd == "get_info":
            return {"status": "ok", "data": {
                "module":             self.module_name,
                "rep_port":           self.rep_port,
                "pub_port":           self.pub_port,
                "publish_interval_s": self.publish_interval_s,
            }}
        return self.handle_command(cmd, args)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ModuleClient:
    """
    ZMQ client for a single module server.

    Handles the REQ-socket state machine: if a send/recv times out the
    socket is in a bad state and is recreated before the next call.

    Example::

        c = ModuleClient("ctrl", rep_port=5550, pub_port=5551)
        c.send("set_gain", axis="x", value=10.0)
        state = c.get_state()

        def on_stream(msg):          # {"module":..., "ts":..., "data":{...}}
            print(msg["data"])
        c.subscribe(on_stream)
        ...
        c.close()
    """

    def __init__(
        self,
        module_name: str,
        rep_port: int,
        pub_port: int,
        host: str = "localhost",
        timeout_ms: int = 2000,
    ) -> None:
        self.module_name = module_name
        self.rep_port    = rep_port
        self.pub_port    = pub_port
        self.host        = host
        self.timeout_ms  = timeout_ms
        self._lock       = threading.Lock()
        self._ctx        = None
        self._req_sock   = None
        self._sub_running = False
        self._sub_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------

    def send(self, cmd: str, **args) -> dict:
        """
        Send a command to the module and return the reply dict.
        Raises TimeoutError if the module does not respond.
        """
        import zmq
        with self._lock:
            self._ensure_sock()
            try:
                self._req_sock.send(json.dumps({"cmd": cmd, "args": args}).encode())
                return json.loads(self._req_sock.recv())
            except zmq.Again:
                # Socket is now in an inconsistent state — recreate it.
                self._req_sock.close()
                self._req_sock = None
                raise TimeoutError(
                    f"Module {self.module_name!r} did not respond "
                    f"within {self.timeout_ms} ms"
                )

    def ping(self) -> bool:
        """Return True if the module is reachable."""
        try:
            return self.send("ping").get("status") == "ok"
        except Exception:
            return False

    def get_state(self) -> dict:
        """Return the module's current state dict."""
        reply = self.send("get_state")
        if reply.get("status") != "ok":
            raise RuntimeError(reply.get("message", "get_state failed"))
        return reply["data"]

    # ------------------------------------------------------------------
    # Subscription (PUB stream)
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[dict], None], topic: str = "") -> None:
        """
        Start a background thread that delivers PUB-stream messages to
        callback(msg).  msg = {"module":..., "ts":..., "data":{...}}.
        Call unsubscribe() to stop.
        """
        if self._sub_running:
            return
        self._sub_running = True
        self._sub_thread = threading.Thread(
            target=self._sub_loop,
            args=(callback, topic),
            daemon=True,
            name=f"{self.module_name}-sub",
        )
        self._sub_thread.start()

    def unsubscribe(self) -> None:
        self._sub_running = False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.unsubscribe()
        with self._lock:
            if self._req_sock is not None:
                self._req_sock.close()
                self._req_sock = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_sock(self) -> None:
        import zmq
        if self._ctx is None:
            self._ctx = zmq.Context.instance()
        if self._req_sock is None:
            sock = self._ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
            sock.connect(f"tcp://{self.host}:{self.rep_port}")
            self._req_sock = sock

    def _sub_loop(self, callback: Callable[[dict], None], topic: str) -> None:
        import zmq
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.connect(f"tcp://{self.host}:{self.pub_port}")
        sock.setsockopt_string(zmq.SUBSCRIBE, topic)
        sock.setsockopt(zmq.RCVTIMEO, 300)
        try:
            while self._sub_running:
                try:
                    callback(json.loads(sock.recv()))
                except zmq.Again:
                    continue
                except Exception:
                    continue
        finally:
            sock.close()
