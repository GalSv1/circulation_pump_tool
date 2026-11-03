import unittest
import time
import uuid
from unittest.mock import patch

import can

from . import protocol as p
from .device import PumpController, Simulator, sequence


class ProtocolTests(unittest.TestCase):
    def test_emp_command_encoding(self):
        identifier, data = p.command(0x8B, 0x0D, 60)
        self.assertEqual(identifier, 0x18EF8B0D)
        self.assertEqual(data, bytes.fromhex("F5 00 00 78 FF FF FF FF"))

    def test_off_and_limits(self):
        self.assertEqual(p.command(0x89, 0xF9, 0)[1], bytes.fromhex("F0 00 00 00 FF FF FF FF"))
        self.assertEqual(p.duty(0), 0)
        self.assertEqual(p.duty(0.5), 0.5)
        self.assertEqual(p.duty(1), 1)
        self.assertEqual(p.duty(36.5), 36.5)
        for value in (-0.5, 100.5, -1, 101, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                p.duty(value)

    def test_status(self):
        self.assertEqual(p.decode_status(bytes.fromhex("41 72 06 FF FF 3C 00 78")),
                         {"direction": 1, "controller_status": 16, "rpm": 825,
                          "power_w": 60, "measured_duty": 60})


class FakeBus:
    def __init__(self):
        self.messages = []

    def send(self, message, timeout=None):
        self.messages.append(message)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.device = PumpController(self.bus)
        self.pump = self.device.pumps["depump_port"]

    def test_sequence_directions_and_endpoint(self):
        plan = sequence(40, 51, 5, 1, 3, 2)
        self.assertEqual(plan["levels"], [40, 45, 50, 51])
        self.assertEqual(plan["durations"], [1, 2, 2, 3])
        self.assertEqual(plan["duration"], 8)
        descending = sequence(50, 40, 5, 1, 3, 2, 2, True)
        self.assertEqual(descending["levels"], [50, 45, 40, 45, 50])
        self.assertEqual(descending["durations"], [3, 2, 1, 2, 3])
        self.assertEqual(descending["duration"], 22)
        plan = sequence(0, 40, 5, 1, 3, 2)
        self.assertEqual(plan["levels"], [0, 5, 10, 15, 20, 25, 30, 35, 40])
        self.assertEqual(sequence(0, 2, 0.5, 1, 3, 2)["levels"], [0, 0.5, 1, 1.5, 2])
        self.assertEqual(p.command(0x90, 0x0D, 0.5)[1], bytes.fromhex("F5 00 00 01 FF FF FF FF"))
        equal_endpoints = sequence(40, 40, 5, 1, 3, 2)
        self.assertEqual(equal_endpoints["levels"], [40])
        self.assertEqual(equal_endpoints["durations"], [4])
        for kwargs in ({"step": 0}, {"low_time": float("nan")}, {"repeats": 0}, {"repeats": 1.5}):
            with self.assertRaises(ValueError):
                sequence(40, 80, **{"step": 5, "low_time": 1, "high_time": 1, "step_time": 1, **kwargs})

    def test_timing_completion_and_cancel(self):
        with patch("circulationpump.device.time.monotonic", return_value=100):
            self.device.arm(True)
            self.device.start_sequence("depump_port", 40, 50, 5, low_time=1, high_time=3, step_time=2)
        for now, expected in ((100.9, 40), (101, 45), (102.9, 45), (103, 50), (105.9, 50), (106, 0)):
            self.pump["last_seen"] = now
            self.device._tick(now)
            self.assertEqual(self.pump["commanded_duty"], expected)
        self.assertIsNone(self.pump["job"])
        self.device.start_sequence("depump_port")
        self.device.manual("depump_port", 0)
        self.assertIsNone(self.pump["job"])

    def test_arm_timeout_fault_and_close(self):
        with self.assertRaises(ValueError):
            self.device.manual("depump_port", 60)
        with patch("circulationpump.device.time.monotonic", return_value=100):
            self.device.arm(True)
            self.device.manual("depump_port", 60)
        self.device._tick(104)
        self.assertEqual(self.pump["commanded_duty"], 0)
        self.device.manual("depump_port", 60)
        self.pump["status"] = {"controller_status": 25, "direction": 1}
        self.device._tick(105)
        self.assertEqual(self.pump["commanded_duty"], 0)
        self.device.close()
        self.assertFalse(self.device.armed)
        self.assertEqual(self.bus.messages[-1].data[0] & 15, 0)

    def test_browser_lease(self):
        self.device.browser_lease = True
        with patch("circulationpump.device.time.monotonic", return_value=100):
            self.device.arm(True)
            self.device.manual("depump_port", 60)
        self.device._tick(104)
        self.assertFalse(self.device.armed)
        self.assertEqual(self.pump["commanded_duty"], 0)

    def test_failed_send_latches_all_commands_off(self):
        self.device.arm(True)
        self.device.manual("gcpump_port", 60)
        with patch.object(self.bus, "send", side_effect=can.CanError("Disconnected")):
            with self.assertRaises(can.CanError):
                self.device.manual("depump_port", 70)
        self.assertFalse(self.device.armed)
        self.assertEqual(self.device.error, "Disconnected")
        self.assertTrue(all(pump["commanded_duty"] == 0 for pump in self.device.pumps.values()))
        with self.assertRaises(ValueError):
            self.device.arm(True)

    def test_virtual_bus_speed_feedback(self):
        channel = f"pump-test-{uuid.uuid4()}"
        with can.Bus(interface="virtual", channel=channel) as client_bus, can.Bus(interface="virtual", channel=channel) as simulator_bus:
            simulator = Simulator(simulator_bus)
            simulator.start()
            try:
                identifier, data = p.command(0x8B, 0x0D, 60)
                client_bus.send(can.Message(arbitration_id=identifier, data=data, is_extended_id=True))
                deadline = time.monotonic() + 2
                feedback = None
                while time.monotonic() < deadline:
                    message = client_bus.recv(0.3)
                    if message and message.arbitration_id == 0x18FF038B:
                        status = p.decode_status(message.data)
                        if status["direction"] == 1:
                            feedback = status
                            break
                self.assertIsNotNone(feedback)
                self.assertEqual(feedback["rpm"], 3300)
                self.assertEqual(feedback["measured_duty"], 60)
            finally:
                simulator.close()

    def test_scan_discovers_unknown_address_without_transmitting(self):
        message = can.Message(arbitration_id=0x18FF0390, data=bytes.fromhex("40 00 00 FF FF 00 00 00"), is_extended_id=True)
        self.pump["last_seen"] = 90
        self.pump["owned"] = True
        def receive_during_scan(seconds):
            self.device._receive(message, 101)
            self.device._tick(101)
            with self.assertRaises(ValueError):
                self.device.arm(True)
        with patch("circulationpump.device.time.monotonic", return_value=100), patch.object(self.device.closed, "wait", side_effect=receive_during_scan):
            result = self.device.scan(1)
        self.assertEqual([pump["address"] for pump in result["pumps"]], [0x90])
        self.assertEqual(self.bus.messages, [])
        self.assertFalse(self.device.scanning)
        self.assertEqual(self.device.scan_result["names"], ["pump_90"])
        self.device.arm(True)
        self.device.manual("0x90", 60)
        self.assertEqual(self.bus.messages[-1].arbitration_id, 0x18EF900D)

    def test_scan_rejects_bad_frames_and_empty_results(self):
        self.device.scanning = True
        for identifier, extended, remote, data in (
            (0x123, False, False, bytes(8)), (0x18FF2390, True, False, bytes(8)),
            (0x18FF0390, True, True, bytes(8)), (0x18FF0390, True, False, bytes(7)),
            (0x18FF03FE, True, False, bytes(8)), (0x18FF030D, True, False, bytes(8))):
            self.device._receive(can.Message(arbitration_id=identifier, data=data,
                                            is_extended_id=extended, is_remote_frame=remote), 100)
        self.assertEqual(len(self.device.pumps), 5)
        self.device.scanning = False
        with patch.object(self.device.closed, "wait"):
            self.assertEqual(self.device.scan(0.1)["pumps"], [])
        self.device.arm(True)
        with self.assertRaises(ValueError):
            self.device.scan()
        self.assertFalse(self.device.scanning)

    def test_numeric_pump_address_without_prior_scan(self):
        self.device.arm(True)
        self.device.manual("0x90", 40)
        self.assertEqual(self.bus.messages[-1].arbitration_id, 0x18EF900D)
        for name in ("0x0D", "0xFE", "unknown"):
            with self.assertRaises(ValueError):
                self.device.manual(name, 40)


if __name__ == "__main__":
    unittest.main()