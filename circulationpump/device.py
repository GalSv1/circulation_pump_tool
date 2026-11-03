"""Periodic pump commands, bounded duty sequences, and status supervision."""

import threading
import time
from collections import deque

import can

from . import protocol as p


def sequence(start, end, step, low_time, high_time, step_time, repeats=1, return_trip=False):
    start, end = p.duty(start), p.duty(end)
    step = p.number(step, "Step")
    low_time = p.number(low_time, "Low-duty hold time")
    high_time = p.number(high_time, "High-duty hold time")
    step_time = p.number(step_time, "Intermediate-step hold time")
    if not 0.5 <= step <= 100 or any(not 0.1 <= value <= 86400 for value in (low_time, high_time, step_time)):
        raise ValueError("Step must be 0.5..100 percent and hold times must be 0.1..86400 seconds")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 10000:
        raise ValueError("Repeats must be an integer in 1..10000")
    if not isinstance(return_trip, bool):
        raise ValueError("Return trip must be a boolean")
    direction = 1 if end >= start else -1
    levels = [start]
    current = start
    while direction * (end - current) > step:
        current += direction * step
        value = p.duty(current)
        if value != levels[-1]:
            levels.append(value)
    if end != levels[-1]:
        levels.append(end)
    if return_trip:
        levels += levels[-2::-1]
    low, high = min(start, end), max(start, end)
    if low == high:
        durations = [low_time + high_time for _level in levels]
    else:
        durations = [low_time if level == low else high_time if level == high else step_time
                     for level in levels]
    return {"levels": levels, "durations": durations, "low_time": low_time,
            "high_time": high_time, "step_time": step_time, "repeats": repeats,
            "duration": sum(durations) * repeats}


class PumpController:
    def __init__(self, bus, source=0x0D, simulated=False, browser_lease=False):
        self.bus = bus
        self.source = p.address(source)
        if self.source in {address for address, _label in p.PUMPS.values()}:
            raise ValueError("Source address cannot be a pump address")
        self.simulated = simulated
        self.browser_lease = browser_lease
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.armed = False
        self.lease = 0
        self.error = None
        self.scanning = False
        self.scan_result = None
        self.log = deque(maxlen=100)
        self.pumps = {name: {"name": name, "label": label, "address": address,
                             "commanded_duty": 0, "owned": False, "job": None,
                             "status": None, "last_seen": None, "active_since": None}
                      for name, (address, label) in p.PUMPS.items()}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _record(self, message):
        self.log.append({"time": time.time(), "message": message})

    def heartbeat(self):
        with self.lock:
            self.lease = time.monotonic()

    def arm(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Armed must be a boolean")
        with self.lock:
            if enabled and self.scanning:
                raise ValueError("Wait for the scan to finish before arming")
            if enabled and self.error:
                raise ValueError("Restart the tool after a CAN communication error")
            if not enabled:
                self._stop_all()
            self.armed = enabled
            self.lease = time.monotonic()

    def _pump(self, name):
        if isinstance(name, str) and name in self.pumps:
            return self.pumps[name]
        try:
            address = int(name[5:], 16) if isinstance(name, str) and name.startswith("pump_") else int(name, 0)
        except (ValueError, TypeError):
            raise ValueError("Choose a pump name or CAN address, e.g. 0x90") from None
        p.address(address)
        if address == self.source:
            raise ValueError("Pump address cannot equal the master's source address")
        return self._register_pump(address)

    def _register_pump(self, address):
        for pump in self.pumps.values():
            if pump["address"] == address:
                return pump
        name = f"pump_{address:02x}"
        pump = {"name": name, "label": f"EMP-compatible pump 0x{address:02X}", "address": address,
                "commanded_duty": 0, "owned": False, "job": None,
                "status": None, "last_seen": None, "active_since": None}
        self.pumps[name] = pump
        return pump

    def scan(self, seconds=3):
        seconds = p.number(seconds, "Scan duration")
        if not 0.1 <= seconds <= 30:
            raise ValueError("Scan duration must be 0.1..30 seconds")
        with self.lock:
            if self.scanning or self.armed:
                raise ValueError("Disarm pump control before scanning; only one scan can run")
            if self.error or self.closed.is_set():
                raise ValueError("Restart the tool before scanning after a CAN error or shutdown")
            self.scanning = True
            started = time.monotonic()
        try:
            self.closed.wait(seconds)
            with self.lock:
                if self.error or self.closed.is_set():
                    raise ValueError(self.error or "Scan interrupted by shutdown")
                found = [pump["name"] for pump in self.pumps.values()
                         if pump["last_seen"] is not None and pump["last_seen"] >= started]
                self.scan_result = {"names": found, "seconds": seconds}
                self._record(f"Scan: {len(found)} EMP-compatible pump address(es) observed")
                return {"seconds": seconds, "pumps": [row for row in self.state()["pumps"] if row["name"] in found]}
        finally:
            with self.lock:
                self.scanning = False

    def _check_start(self, pump):
        if not self.armed or self.error:
            raise ValueError("Arm pump control before starting")
        status = pump["status"]
        if status and (status["controller_status"] in p.CRITICAL_STATUSES or status["direction"] == 2):
            raise ValueError("Pump reports a fault or reverse rotation")

    def _send(self, pump):
        identifier, data = p.command(pump["address"], self.source, pump["commanded_duty"])
        try:
            self.bus.send(can.Message(arbitration_id=identifier, data=data, is_extended_id=True), timeout=0.1)
        except (can.CanError, OSError) as exc:
            self.error = str(exc)
            self.armed = False
            for controlled in self.pumps.values():
                controlled["commanded_duty"] = 0
                controlled["job"] = None
                controlled["active_since"] = None
            raise

    def _set(self, pump, percent):
        if percent and not pump["commanded_duty"]:
            pump["active_since"] = time.monotonic()
        if not percent:
            pump["active_since"] = None
        pump["commanded_duty"] = percent
        pump["owned"] = True
        self._send(pump)

    def manual(self, name, percent):
        percent = p.duty(percent)
        with self.lock:
            pump = self._pump(name)
            if percent:
                self._check_start(pump)
            pump["job"] = None
            self._set(pump, percent)
            self._record(f"{name}: commanded {percent:g}%")

    def start_sequence(self, name, start=40, end=80, step=5, low_time=5, high_time=5,
                       step_time=5, repeats=1, return_trip=False):
        plan = sequence(start, end, step, low_time, high_time, step_time, repeats, return_trip)
        with self.lock:
            pump = self._pump(name)
            self._check_start(pump)
            self._set(pump, plan["levels"][0])
            pump["job"] = {**plan, "started": time.monotonic()}
            self._record(f"{name}: sequence started ({plan['duration']:g} s)")
        return plan

    def _stop_all(self):
        failure = None
        for pump in self.pumps.values():
            if pump["owned"]:
                pump["job"] = None
                try:
                    self._set(pump, 0)
                except (can.CanError, OSError) as exc:
                    failure = exc
        if failure:
            self.error = str(failure)
            self._record(f"Stop transmission failed: {failure}")

    def stop_all(self):
        with self.lock:
            self.armed = False
            self._stop_all()
            self._record("All controlled pumps commanded off; disarmed")

    def _receive(self, message, now):
        if not message.is_extended_id or message.is_error_frame or message.is_remote_frame:
            return
        if message.arbitration_id & 0x03FFFF00 != 0x00FF0300 or len(message.data) != 8:
            return
        address = message.arbitration_id & 0xFF
        if address > 0xFD or address == self.source:
            return
        pump = next((pump for pump in self.pumps.values() if pump["address"] == address), None)
        if pump is None and self.scanning:
            pump = self._register_pump(address)
        if pump is not None:
            pump["status"] = p.decode_status(message.data)
            pump["last_seen"] = now

    def _tick(self, now):
        if self.scanning:
            return
        if self.browser_lease and self.armed and now - self.lease > 3:
            self.stop_all()
            self._record("Browser session expired")
        for pump in self.pumps.values():
            if not pump["owned"]:
                continue
            if pump["commanded_duty"]:
                status = pump["status"]
                fault = status and (status["controller_status"] in p.CRITICAL_STATUSES or status["direction"] == 2)
                last = max(pump["last_seen"] or 0, pump["active_since"] or 0)
                if fault or now - last > 3:
                    pump["job"] = None
                    self._set(pump, 0)
                    self._record(f"{pump['name']}: stopped on {'pump fault' if fault else 'status timeout'}")
            job = pump["job"]
            if job:
                elapsed = max(0, now - job["started"])
                cycle_duration = sum(job["durations"])
                if elapsed >= job["duration"]:
                    pump["job"] = None
                    self._set(pump, 0)
                    self._record(f"{pump['name']}: sequence complete, commanded off")
                else:
                    cycle_position = elapsed % cycle_duration
                    position = 0
                    while cycle_position >= job["durations"][position]:
                        cycle_position -= job["durations"][position]
                        position += 1
                    pump["commanded_duty"] = job["levels"][position]
                    if pump["commanded_duty"] and pump["active_since"] is None:
                        pump["active_since"] = now
                    elif not pump["commanded_duty"]:
                        pump["active_since"] = None
            self._send(pump)

    def _run(self):
        deadline = 0
        try:
            while not self.closed.is_set():
                message = self.bus.recv(0.02)
                now = time.monotonic()
                with self.lock:
                    if message:
                        self._receive(message, now)
                    if now >= deadline:
                        self._tick(now)
                        deadline = now + 0.1
        except (can.CanError, OSError) as exc:
            with self.lock:
                self.error = str(exc)
                self.armed = False
                self._record(f"CAN communication failed: {exc}")
                self._stop_all()

    def state(self):
        with self.lock:
            now = time.monotonic()
            rows = []
            for pump in self.pumps.values():
                row = {key: value for key, value in pump.items() if key not in ("job", "active_since", "last_seen")}
                row["status_age"] = None if pump["last_seen"] is None else now - pump["last_seen"]
                job = pump["job"]
                row["sequence"] = None if not job else {
                    "levels": job["levels"], "durations": job["durations"],
                    "repeats": job["repeats"], "duration": job["duration"],
                    "remaining": max(0, job["duration"] - (now - job["started"]))}
                rows.append(row)
            return {"armed": self.armed, "simulated": self.simulated, "source": self.source,
                    "error": self.error, "scanning": self.scanning, "scan_result": self.scan_result,
                    "pumps": rows, "log": list(self.log)}

    def close(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.stop_all()


class Simulator:
    def __init__(self, bus):
        self.bus = bus
        self.duties = {address: 0 for address, _label in p.PUMPS.values()}
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        deadline = 0
        while not self.closed.is_set():
            message = self.bus.recv(0.02)
            if message and message.is_extended_id and not message.is_remote_frame and len(message.data) == 8:
                destination = (message.arbitration_id >> 8) & 0xFF
                if message.arbitration_id & 0x03FF0000 == 0x00EF0000 and destination in self.duties:
                    self.duties[destination] = message.data[3] / 2 if message.data[0] & 3 == 1 else 0
            now = time.monotonic()
            if now >= deadline:
                for address, percent in self.duties.items():
                    rpm = round(percent * 55 * 2)
                    power = round(percent)
                    data = bytes([0x41 if percent else 0x40]) + rpm.to_bytes(2, "little") + bytes([255, 255]) + power.to_bytes(2, "little") + bytes([int(percent * 2)])
                    self.bus.send(can.Message(arbitration_id=0x18FF0300 | address, data=data, is_extended_id=True))
                deadline = now + 0.2

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)