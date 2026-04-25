"""
ctrl_cli.py  —  usphere-CTRL terminal interface

Connects to a running ctrl_server.py and sends commands.

One-shot usage::

    python ctrl_cli.py ping
    python ctrl_cli.py connect
    python ctrl_cli.py snapshot
    python ctrl_cli.py read  gain_x
    python ctrl_cli.py write gain_x 10.5
    python ctrl_cli.py get_tic
    python ctrl_cli.py tic_command start_pump
    python ctrl_cli.py ramp gain_x 15.0 0.5 0.05
    python ctrl_cli.py list_modules
    python ctrl_cli.py test_module EDWARDS_TIC

Interactive REPL::

    python ctrl_cli.py --interactive
    python ctrl_cli.py -i

Global options::

    --host HOST    server hostname (default: localhost)
    --rep PORT     REP port (default: 5550)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmq_base import ModuleClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_reply(reply: dict) -> None:
    status = reply.get("status", "?")
    if status == "error":
        print(f"ERROR: {reply.get('message', reply)}")
    else:
        data = reply.get("data")
        if data is None:
            print("OK")
        elif isinstance(data, dict):
            print(json.dumps(data, indent=2, default=str))
        else:
            print(data)


def _client(host: str, rep_port: int) -> ModuleClient:
    return ModuleClient("ctrl", rep_port=rep_port, pub_port=rep_port + 1,
                        host=host, timeout_ms=5000)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _run_one(client: ModuleClient, tokens: list[str]) -> bool:
    """
    Execute one command from a list of string tokens.
    Returns False to quit the REPL, True to continue.
    """
    if not tokens:
        return True

    cmd = tokens[0].lower()

    if cmd in ("quit", "exit", "q"):
        return False

    if cmd == "help":
        print(__doc__)
        return True

    if cmd == "ping":
        ok = client.ping()
        print("ONLINE" if ok else "OFFLINE")
        return True

    if cmd == "get_state":
        _print_reply(client.send("get_state"))
        return True

    if cmd == "connect":
        _print_reply(client.send("connect"))
        return True

    if cmd == "disconnect":
        _print_reply(client.send("disconnect"))
        return True

    if cmd == "snapshot":
        _print_reply(client.send("snapshot"))
        return True

    if cmd == "read":
        if len(tokens) < 2:
            print("Usage: read <register_name>")
            return True
        _print_reply(client.send("read_register", name=tokens[1]))
        return True

    if cmd == "write":
        if len(tokens) < 3:
            print("Usage: write <register_name> <value>")
            return True
        try:
            value = float(tokens[2])
        except ValueError:
            print("Value must be a number")
            return True
        _print_reply(client.send("write_register", name=tokens[1], value=value))
        return True

    if cmd == "get_tic":
        _print_reply(client.send("get_tic"))
        return True

    if cmd == "tic_command":
        if len(tokens) < 2:
            print("Usage: tic_command <start_pump|stop_pump|set_speed> [speed_pct]")
            return True
        kwargs: dict = {"action": tokens[1]}
        if tokens[1] == "set_speed" and len(tokens) >= 3:
            kwargs["speed_pct"] = int(tokens[2])
        _print_reply(client.send("tic_command", **kwargs))
        return True

    if cmd == "set_tic_config":
        if len(tokens) < 2:
            print("Usage: set_tic_config <port> [baud_rate]")
            return True
        kwargs = {"port": tokens[1]}
        if len(tokens) >= 3:
            kwargs["baud_rate"] = tokens[2]
        _print_reply(client.send("set_tic_config", **kwargs))
        return True

    if cmd == "start_tic_poll":
        interval = float(tokens[1]) if len(tokens) >= 2 else 2.0
        _print_reply(client.send("start_tic_poll", interval_s=interval))
        return True

    if cmd == "stop_tic_poll":
        _print_reply(client.send("stop_tic_poll"))
        return True

    if cmd == "ramp":
        # ramp <name> <target> <step> <delay_s>
        if len(tokens) < 5:
            print("Usage: ramp <register_name> <target> <step> <delay_s>")
            return True
        _print_reply(client.send("ramp",
            name=tokens[1], target=float(tokens[2]),
            step=float(tokens[3]), delay_s=float(tokens[4]),
        ))
        return True

    if cmd == "list_modules":
        _print_reply(client.send("list_modules"))
        return True

    if cmd == "test_module":
        if len(tokens) < 2:
            print("Usage: test_module <MODULE_NAME>")
            return True
        _print_reply(client.send("test_module", name=tokens[1]))
        return True

    if cmd == "read_module":
        if len(tokens) < 2:
            print("Usage: read_module <MODULE_NAME>")
            return True
        _print_reply(client.send("read_module", name=tokens[1]))
        return True

    if cmd == "change_pars":
        print("change_pars requires JSON args — use the Python API or GUI for this command.")
        return True

    # Fall through: forward raw command to server
    print(f"Sending raw command {cmd!r} ...")
    _print_reply(client.send(cmd))
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="usphere-CTRL terminal interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--rep",  type=int, default=5550)
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="start an interactive REPL")
    parser.add_argument("command", nargs="*", help="command and arguments")
    args = parser.parse_args()

    client = _client(args.host, args.rep)

    if args.interactive or not args.command:
        # REPL
        print(f"ctrl-cli  connected to {args.host}:{args.rep}")
        print("Type 'help' for commands, 'quit' to exit.\n")
        while True:
            try:
                line = input("ctrl> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if not _run_one(client, line.split()):
                break
    else:
        _run_one(client, args.command)

    client.close()


if __name__ == "__main__":
    main()
