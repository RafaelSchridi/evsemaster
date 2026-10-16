"""Record every frame chargers broadcast to 28376 as JSON lines, optionally sending read-only requests first.

    poe capture --seconds 60 --out cap.jsonl
    poe capture --seconds 60 --out cap.jsonl STATUS_REQUEST 0x810D:02 HISTORY_RECORD_REQUEST:0100000001

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

from evsemaster import Action, CommandEnum
from evsemaster.data_types import ENVELOPE_SIZE, HEADER
from evsemaster.protocol import build_packet

LISTEN_PORT = 28376
QUIET = {CommandEnum.HEADING_EVENT}


def name(code: int) -> str:
    try:
        return CommandEnum(code).name
    except ValueError:
        return f"UNKNOWN_{code:#06x}"


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

    def record(self, **rec) -> None:
        self.out.write(json.dumps(rec) + "\n")
        self.out.flush()

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < ENVELOPE_SIZE or struct.unpack(">H", data[:2])[0] != HEADER:
            return
        code = struct.unpack(">H", data[19:21])[0]
        declared = struct.unpack(">H", data[2:4])[0]
        end = declared if ENVELOPE_SIZE <= declared <= len(data) else len(data)
        payload = data[21 : end - 4]
        serial = data[5:13].hex()
        if addr[0] == self.target_ip and serial != "0" * 16 and not self.learned.is_set():
            self.serial, self.port = serial, addr[1]
            self.learned.set()
        shown = "<redacted: Wi-Fi config>" if code == CommandEnum.WIFI_CONFIG_EVENT else payload.hex()
        self.record(
            t=round(time.time(), 3),
            dir="in",
            src=f"{addr[0]}:{addr[1]}",
            serial=serial,
            cmd=f"{code:#06x}",
            name=name(code),
            len=len(payload),
            payload=shown,
        )
        if code in QUIET:
            self.headings += 1
            return
        print(f"{time.strftime('%H:%M:%S')} {addr[0]}:{addr[1]} {name(code)} ({code:#06x}) {len(payload)}B {shown}")


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
            print(f"{cap.headings} headings seen")


if __name__ == "__main__":
    asyncio.run(main())
