"""Print one line per charge record in 0x000C history replies from tools/capture.py files, then tallies.

poetry run python tools/decode_history.py cap.jsonl [...]
"""

import json
import struct
import sys
from collections import Counter
from datetime import UTC, datetime

SHANGHAI_OFFSET = 28800


def wall_clock(epoch: int) -> str:
    if epoch in (0, 0xFFFFFFFF):
        return "-"
    return datetime.fromtimestamp(epoch + SHANGHAI_OFFSET, UTC).strftime("%Y-%m-%d %H:%M")


def text(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("ascii", "replace").strip()


def main() -> None:
    slots: dict[int, bytes] = {}
    for path in sys.argv[1:]:
        with open(path) as f:
            for line in f:
                frame = json.loads(line)
                if frame["dir"] != "in" or frame["cmd"] != "0x000c":
                    continue
                payload = bytes.fromhex(frame["payload"])
                index, flag = struct.unpack(">I", payload[4:8])[0], payload[8]
                if flag == 1 and len(payload) >= 9 + 96:
                    slots[index] = payload[9:]

    start_types, charge_types, stop_reasons = Counter(), Counter(), Counter()
    for index in sorted(slots):
        rec = slots[index]
        reserved, start_type, charge_type = rec[49], rec[50], rec[51]
        max_minutes, max_energy, _ = struct.unpack(">HHH", rec[52:58])
        stop_reason, has_stop = rec[58], rec[59]
        reservation, start, stop, duration = struct.unpack(">IIII", rec[60:76])
        energy = struct.unpack(">I", rec[84:88])[0] / 100
        start_types[start_type] += 1
        charge_types[charge_type] += 1
        stop_reasons[stop_reason] += 1
        print(
            f"{index:3} start={start_type} charge={charge_type} reason={stop_reason:3} has_stop={has_stop} "
            f"reserved={reserved} at={wall_clock(reservation)} limits=({max_minutes},{max_energy}) "
            f"{wall_clock(start)} -> {wall_clock(stop)} {duration / 60:7.1f}m {energy:6.2f}kWh "
            f"by={text(rec[1:17])!r} stopped_by={text(rec[17:33])!r} id={text(rec[33:49])!r}"
        )

    print(f"\nstart types: {dict(start_types)}\ncharge types: {dict(charge_types)}\nstop reasons: {dict(stop_reasons)}")


if __name__ == "__main__":
    main()
