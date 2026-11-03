"""Circulation pump manual control and repeatable duty scripts."""

import argparse
import json
import sys
import time
import uuid
import webbrowser
from contextlib import ExitStack

import can

from . import protocol as p
from .device import PumpController, Simulator, sequence
from .server import make_server


def build_parser():
    parser = argparse.ArgumentParser(description="EMP circulation pump bench tool (retraction CAN, 250 kbit/s)")
    parser.add_argument("-c", "--channel", default="can0")
    parser.add_argument("-i", "--interface", default="socketcan")
    parser.add_argument("-b", "--bitrate", type=int, default=250000)
    parser.add_argument("-s", "--source", type=lambda value: int(value, 0), default=0x0D,
                        help="Master source address; default matches RCU 0x0D for a standalone pump.")
    parser.add_argument("--simulate", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    ui = commands.add_parser("ui")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8326)
    ui.add_argument("--open", action="store_true")
    commands.add_parser("status")
    scan = commands.add_parser("scan", help="Listen for EMP pump status without transmitting probes")
    scan.add_argument("--seconds", type=float, default=3)
    stop = commands.add_parser("stop")
    stop.add_argument("pump", help="Pump name, CAN address (e.g. 0x90), or all")
    stop.add_argument("--yes", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("pump", help="Pump name or CAN address (e.g. 0x90)")
    run.add_argument("duty", type=float)
    run.add_argument("--seconds", type=float, default=10)
    run.add_argument("--yes", action="store_true")
    script = commands.add_parser("cycle")
    script.add_argument("pump", help="Pump name or CAN address (e.g. 0x90)")
    script.add_argument("--start", type=float, default=40)
    script.add_argument("--end", type=float, default=80)
    script.add_argument("--step", type=float, default=5)
    script.add_argument("--low-time", type=float, default=5, help="Seconds to hold the lower endpoint duty")
    script.add_argument("--high-time", type=float, default=5, help="Seconds to hold the upper endpoint duty")
    script.add_argument("--step-time", type=float, default=5, help="Seconds to hold intermediate duty steps")
    script.add_argument("--repeats", type=int, default=1)
    script.add_argument("--return-trip", action="store_true")
    script.add_argument("--script", help="JSON file with start, end, step, low_time, high_time, step_time")
    script.add_argument("--yes", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ("run", "cycle", "stop") and not args.simulate and not args.yes:
        parser.error("Live control requires --yes; confirm the pump is disconnected from the RCU and primed")
    try:
        if args.command == "run":
            seconds = p.number(args.seconds, "Duration")
            if not 0.1 <= seconds <= 86400:
                raise ValueError("Duration must be 0.1..86400 seconds")
            p.duty(args.duty)
        if args.command == "cycle":
            config = {"start": args.start, "end": args.end, "step": args.step,
                      "low_time": args.low_time, "high_time": args.high_time,
                      "step_time": args.step_time, "repeats": args.repeats,
                      "return_trip": args.return_trip}
            if args.script:
                with open(args.script) as script_file:
                    config = json.load(script_file)
                if not isinstance(config, dict) or set(config) - {"start", "end", "step", "dwell", "low_time", "high_time", "step_time", "repeats", "return_trip"}:
                    raise ValueError("Invalid sequence JSON fields")
                legacy_dwell = config.pop("dwell", 5)
                for key in ("low_time", "high_time", "step_time"):
                    config.setdefault(key, legacy_dwell)
            sequence(**config)
        with ExitStack() as stack:
            channel = f"pump-{uuid.uuid4()}" if args.simulate else args.channel
            interface = "virtual" if args.simulate else args.interface
            options = {} if interface in ("virtual", "socketcan") else {"bitrate": args.bitrate}
            bus = stack.enter_context(can.Bus(interface=interface, channel=channel, **options))
            if args.simulate:
                simulator_bus = stack.enter_context(can.Bus(interface="virtual", channel=channel))
                simulator = Simulator(simulator_bus)
                simulator.start()
                stack.callback(simulator.close)
            controller = PumpController(bus, args.source, args.simulate, browser_lease=args.command == "ui")
            controller.start()
            stack.callback(controller.close)
            if args.command == "ui":
                server = make_server(controller, args.host, args.port)
                stack.callback(server.server_close)
                url = f"http://{args.host}:{server.server_port}/"
                print(f"Circulation pumps {'SIMULATED' if args.simulate else args.channel}: {url}", flush=True)
                if args.open:
                    webbrowser.open(url)
                server.serve_forever()
            elif args.command == "status":
                controller.closed.wait(1.5)
                print(json.dumps(controller.state(), indent=2))
            elif args.command == "scan":
                print(json.dumps(controller.scan(args.seconds), indent=2))
            elif args.command == "stop":
                names = p.PUMPS if args.pump == "all" else [args.pump]
                for name in names:
                    controller.manual(name, 0)
                controller.closed.wait(0.5)
                print("Off commands sent; check pump status to verify physical stop.")
            else:
                args.pump = controller._pump(args.pump)["name"]
                controller.arm(True)
                if args.command == "run":
                    controller.manual(args.pump, args.duty)
                    deadline = time.monotonic() + seconds
                else:
                    plan = controller.start_sequence(args.pump, **config)
                    print(json.dumps(plan, indent=2), flush=True)
                    deadline = time.monotonic() + plan["duration"]
                while time.monotonic() < deadline:
                    controller.closed.wait(min(0.2, max(0, deadline - time.monotonic())))
                    state = controller.state()
                    pump = next(row for row in state["pumps"] if row["name"] == args.pump)
                    if state["error"]:
                        raise ValueError(state["error"])
                    if args.command == "run" and args.duty and not pump["commanded_duty"]:
                        raise ValueError("Pump stopped by status supervision")
                    if args.command == "cycle" and not pump["sequence"] and time.monotonic() < deadline - 0.15:
                        raise ValueError("Sequence stopped by status supervision")
                controller.manual(args.pump, 0)
                print("Finished; pump commanded off.")
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, can.CanError, OSError) as exc:
        print(f"Circulation pumps: {exc}", file=sys.stderr)
        return 1