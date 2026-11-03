# EMP Circulation Pump Tool

Standalone bench utility for monitoring and controlling EMP circulation pumps
over a directly connected CAN adapter. The pump must be disconnected from the
RCU while this tool is controlling it.

## Install

Linux with SocketCAN:

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

## Safety

- Keep the pump electrically and logically disconnected from the RCU during
  standalone control. The tool bypasses RCU thermal management, ramp limiting,
  and leakage monitoring.
- Power the pump from a fused supply that matches its rating; the CAN adapter
  does not supply pump power. Follow the pump and adapter wiring specifications
  for CAN_H, CAN_L, reference, and termination.
- Prime the cooling circuit before operation. Never run the pump dry.
- Keep a physical power disconnect available. Software cannot guarantee an
  off command after a CAN failure, process termination, or power loss.
- Verify low-duty behavior on the bench; commanded duty is not proof of actual
  pump operation.

Tests:

```bash
python3 -m unittest circulationpump.test_pump -v
```