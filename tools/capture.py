"""Record every datagram chargers broadcast to 28376 as JSON lines, frame by frame, optionally sending read-only
requests first.

    poe capture --seconds 60 --out cap.jsonl
    poe capture --seconds 60 --out cap.jsonl STATUS_REQUEST 0x810D:02 HISTORY_RECORD_REQUEST:0100000001

Each frame record gives its datagram's frame count, its index there, its declared and actual size, whether tail and
checksum hold, and any bytes past the declared end. Bytes that aren't a frame are kept as raw hex, with `cmd` None.

Requests need EVSE=<host>:<password>, which `poe capture` reads from .env. Never logs in or answers headings, so a
client that holds the registration keeps it. The port is exclusive: stop test.py or a local HA first.
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time
from collections import Counter

from evsemaster import Action, CommandEnum
from evsemaster.data_types import ENVELOPE_SIZE, HEADER, TAIL
from evsemaster.protocol import build_packet, split_frames

LISTEN_PORT = 28376
QUIET = {CommandEnum.HEADING_EVENT}
HEADER_BYTES = HEADER.to_bytes(2)
WIFI_CONFIG_BYTES = CommandEnum.WIFI_CONFIG_EVENT.to_bytes(2)
REDACTED = "<redacted: Wi-Fi config>"


def name(code: int) -> str:
    try:
        return CommandEnum(code).name
    except ValueError:
        return f"UNKNOWN_{code:#06x}"


def redact(data: bytes) -> str:
    """Hex of the bytes, unless a Wi-Fi config reply starts anywhere inside."""
    starts = [i for i in range(len(data)) if data.startswith(HEADER_BYTES, i)]
    if any(data[i + 19 : i + 21] == WIFI_CONFIG_BYTES for i in starts):
        return REDACTED
    return data.hex()


def parse_request(arg: str) -> tuple[CommandEnum, bytes]:
    """Parse `CMD` or `CMD:hexpayload`; a settings request goes out as a get and may not be a set."""
    cmd_str, _, hex_payload = arg.partition(":")
    try:
        cmd = CommandEnum[cmd_str.upper()]
    except KeyError:
        cmd = CommandEnum(int(cmd_str, 0))
    payload = bytes.fromhex(hex_payload)
    if cmd & 0xFF00 == 0x8100 and cmd != CommandEnum.VERSION_REQUEST:
        payload = payload or bytes([Action.GET])
        if payload[0] != Action.GET:
            sys.exit(f"refusing {cmd.name}: settings requests are sent as gets only (first byte 02)")
    return cmd, payload


class Capture(asyncio.DatagramProtocol):
    def __init__(self, out, target_ip: str | None):
        self.out = out
        self.target_ip = target_ip
        self.serial: str | None = None
        self.port: int | None = None
        self.learned = asyncio.Event()
        self.headings = 0
        self.counts = Counter()
        # sources whose last frame was a Wi-Fi config reply cut short: the rest carries the passphrase without a header
        self.wifi_cut: set[str] = set()

    def record(self, **rec) -> None:
        self.out.write(json.dumps(rec) + "\n")
        self.out.flush()

    def datagram_received(self, data: bytes, addr) -> None:
        frames = split_frames(data)
        if len(frames) > 1:
            self.counts["combined datagrams"] += 1
        now, src = round(time.time(), 3), f"{addr[0]}:{addr[1]}"
        for index, frame in enumerate(frames):
            where = dict(t=now, dir="in", src=src, frames=len(frames), index=index, size=len(frame))
            if len(frame) < ENVELOPE_SIZE or not frame.startswith(HEADER_BYTES):
                self.counts["not a frame"] += 1
                raw = REDACTED if src in self.wifi_cut else redact(frame)
                self.wifi_cut.discard(src)
                self.record(**where, cmd=None, raw=raw)
                print(f"{time.strftime('%H:%M:%S')} {src} not a frame, {len(frame)}B")
                continue
            self.wifi_cut.discard(src)
            code = struct.unpack(">H", frame[19:21])[0]
            declared = struct.unpack(">H", frame[2:4])[0]
            end = declared if ENVELOPE_SIZE <= declared <= len(frame) else len(frame)
            checksum, tail = struct.unpack(">HH", frame[end - 4 : end])
            valid = tail == TAIL and checksum == sum(frame[: end - 4]) % 0xFFFF
            if declared != len(frame):
                self.counts["size differs from declared"] += 1
            if not valid:
                self.counts["bad tail or checksum"] += 1
            payload = frame[21 : end - 4]
            serial = frame[5:13].hex()
            if addr[0] == self.target_ip and serial != "0" * 16 and not self.learned.is_set():
                self.serial, self.port = serial, addr[1]
                self.learned.set()
            wifi = code == CommandEnum.WIFI_CONFIG_EVENT
            if wifi and declared > len(frame):
                self.wifi_cut.add(src)
            shown = REDACTED if wifi else payload.hex()
            extra = frame[end:]
            self.record(
                **where,
                serial=serial,
                cmd=f"{code:#06x}",
                name=name(code),
                len=len(payload),
                payload=shown,
                declared=declared,
                valid=valid,
                extra=REDACTED if wifi and extra else redact(extra),
            )
            odd = len(frames) > 1 or declared != len(frame) or not valid
            if code in QUIET:
                self.headings += 1
                if not odd:
                    continue
            note = ""
            if odd:
                verdict = "ok" if valid else "BAD"
                note = f" [frame {index + 1}/{len(frames)}, declared {declared}, got {len(frame)}, {verdict}]"
            print(f"{time.strftime('%H:%M:%S')} {src} {name(code)} ({code:#06x}) {len(payload)}B {shown}{note}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=60, help="how long to record")
    parser.add_argument("--out", required=True, help="JSONL file to append to")
    parser.add_argument("--gap", type=float, default=1.5, help="seconds between requests")
    parser.add_argument("requests", nargs="*", help="CMD or CMD:hexpayload, sent in order")
    args = parser.parse_args()
    requests = [parse_request(r) for r in args.requests]

    ip, _, password = os.environ.get("EVSE", "").partition(":")
    if requests and not (ip and password):
        sys.exit("set EVSE=<host>:<password> to send requests")

    loop = asyncio.get_running_loop()
    with open(args.out, "a") as out:
        cap = Capture(out, ip or None)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: cap, local_addr=("0.0.0.0", LISTEN_PORT), allow_broadcast=True
        )
        deadline = loop.time() + args.seconds
        try:
            if requests:
                print(f"waiting for a frame from {ip} to learn its serial and port...")
                await asyncio.wait_for(cap.learned.wait(), timeout=args.seconds)
                print(f"charger {cap.serial} at {ip}:{cap.port}")
                for cmd, payload in requests:
                    transport.sendto(build_packet(cmd, cap.serial, password, payload), (ip, cap.port))
                    cap.record(
                        t=round(time.time(), 3),
                        dir="out",
                        cmd=f"{cmd:#06x}",
                        name=cmd.name,
                        len=len(payload),
                        payload=payload.hex(),
                    )
                    print(f"{time.strftime('%H:%M:%S')} -> {cmd.name} ({cmd:#06x}) {payload.hex()}")
                    await asyncio.sleep(args.gap)
            await asyncio.sleep(max(0, deadline - loop.time()))
        finally:
            transport.close()
            print("seen: " + ", ".join([f"{cap.headings} headings"] + [f"{n} {w}" for w, n in cap.counts.items()]))


if __name__ == "__main__":
    asyncio.run(main())
