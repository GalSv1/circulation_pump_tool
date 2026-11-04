# EMP Circulation Pump Tool

Standalone bench utility for monitoring and controlling EMP circulation pumps
over a directly connected CAN adapter. Make sure to disconnect the pump from the boat's canbus.

## Install

### Windows

Install Python 3.9 or later and the Windows driver/software for your CAN adapter.
For example, PEAK adapters require PEAK's PCAN-Basic driver. In PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m circulationpump --simulate ui --open
```

For a PEAK adapter on the documented 250 kbit/s bus, use its PCAN channel:

```powershell
python -m circulationpump -i pcan -c PCAN_USBBUS1 -b 250000 ui --open
python -m circulationpump -i pcan -c PCAN_USBBUS1 -b 250000 scan --seconds 5
```

The interface name and channel depend on the adapter and its `python-can`
backend. Install any backend-specific package or vendor driver required by
your adapter; SocketCAN (`socketcan`, `can0`) is Linux-only. See the
[`python-can` supported interfaces](https://python-can.readthedocs.io/en/stable/interfaces.html)
for other adapters and their setup requirements.

### Linux with SocketCAN

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 250000
sudo ip link set can0 up
```

Configure the adapter at the pump's CAN bitrate. The documented RCU retraction
network uses 250 kbit/s; the tool does not detect or change bitrate. For
non-SocketCAN adapters, select the python-can interface with `-i` and channel
with `-c`; `-b` sets bitrate for adapters that accept it.

## Use

```bash
python3 -m circulationpump --simulate ui --open
python3 -m circulationpump -c can0 ui --open
python3 -m circulationpump -c can0 scan --seconds 5
python3 -m circulationpump -c can0 status
python3 -m circulationpump -c can0 run 0x8B 60 --seconds 30 --yes
python3 -m circulationpump -c can0 cycle 0x8B --start 40 --end 80 --step 5 \
  --low-time 10 --high-time 20 --step-time 2 --repeats 3 --return-trip --yes
python3 -m circulationpump -c can0 cycle 0x8B \
  --script circulationpump/cycle.example.json --yes
python3 -m circulationpump -c can0 stop 0x8B --yes
python3 -m circulationpump -c can0 stop all --yes
```

The browser UI defaults to `http://127.0.0.1:8326/`. It starts disarmed. Scan
listens for EMP-compatible status messages without sending probes. A silent
pump cannot be found by passive scan; numeric addresses can still be entered
directly. Known boat mapping: battery/DE starboard `0x89`, propulsion/GC
starboard `0x8A`, battery/DE port `0x8B`, propulsion/GC port `0x8C`, cabin
circulation `0x8D`. These labels are address mappings, not pump identities.

Duty commands accept 0-100% in 0.5% increments. Zero sends the motor-off
command; nonzero values are sent as running-duty requests. The RCU firmware's
36.5% minimum-on clamp does not apply in this standalone tool. Physical pump
behavior at low duty should be verified on a primed bench setup before use.

Sequence low/high hold times apply to the numeric low/high duty endpoints,
regardless of direction. Intermediate duty values use the step hold time. A
return trip traverses intermediate values in reverse; repeated cycles restart
at the requested start. At completion or cancellation, the tool commands the
pump off. JSON scripts use `start`, `end`, `step`, `low_time`, `high_time`,
`step_time`, `repeats`, and `return_trip`. Older scripts with a single `dwell`
field remain supported.

Tests:

```bash
python3 -m unittest circulationpump.test_pump -v
```