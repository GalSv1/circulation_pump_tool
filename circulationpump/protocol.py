"""EMP MCM/MSM1 messages used by the RCU circulation pump driver."""

import math

PUMPS = {
    "depump_stbd": (0x89, "Battery / DE starboard"),
    "gcpump_stbd": (0x8A, "Propulsion / GC starboard"),
    "depump_port": (0x8B, "Battery / DE port"),
    "gcpump_port": (0x8C, "Propulsion / GC port"),
    "cabinpump": (0x8D, "Cabin circulation"),
}
CRITICAL_STATUSES = {20, 22, 23, 25, 31}


def number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def duty(value):
    result = number(value, "Duty")
    if not 0 <= result <= 100:
        raise ValueError("Duty must be 0..100 percent")
    return math.floor(result * 2 + 0.5) / 2


def address(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFD:
        raise ValueError("J1939 address must be 0x00..0xFD")
    return value


def command(destination, source, percent):
    percent = duty(percent)
    identifier = 0x18EF0000 | (address(destination) << 8) | address(source)
    return identifier, bytes([0xF5 if percent else 0xF0, 0, 0,
                              int(percent * 2), 0xFF, 0xFF, 0xFF, 0xFF])


def decode_status(data):
    if len(data) != 8:
        raise ValueError("MSM1 must contain eight bytes")
    rpm_raw = int.from_bytes(data[1:3], "little")
    power_raw = int.from_bytes(data[5:7], "little")
    return {"direction": data[0] & 3, "controller_status": (data[0] >> 2) & 31,
            "rpm": rpm_raw / 2 if rpm_raw <= 64255 else None,
            "power_w": power_raw if power_raw <= 64255 else None,
            "measured_duty": data[7] / 2 if data[7] <= 250 else None}