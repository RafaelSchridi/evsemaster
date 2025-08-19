"""Test script for EVSE port discovery."""

import asyncio
import logging
import sys
import contextlib
from evsemaster.evse_protocol import SimpleEVSEProtocol
from evsemaster.data_types import CommandEnum
from datetime import datetime

logging.basicConfig(level=logging.DEBUG)


async def periodic_status(protocol: SimpleEVSEProtocol, interval: int = 60):
    """Periodically request status from the EVSE."""
    try:
        while True:
            await protocol.request_status()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # Allow task to be cancelled cleanly
        return


async def command_input_loop(protocol: SimpleEVSEProtocol):
    """Read commands from terminal and send them to the EVSE.

    Accepts either:
    - CommandEnum names (e.g., SINGLE_AC_STATUS, HEADING)
    - Decimal or hex values (e.g., 32772 or 0x8004)
    - Shortcuts: status, start [amps], stop, help, quit/exit
    """
    names = [e.name for e in CommandEnum]
    print("Type 'help' to list commands. Type 'quit' to exit.")
    while True:
        try:
            line = await asyncio.to_thread(input, "evse> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting input loop...")
            return

        if not line:
            continue
        parts = line.strip().split()
        if not parts:
            continue

        cmd_str = parts[0].lower()
        args = parts[1:]

        if cmd_str in ("quit", "exit"):
            return
        if cmd_str == "help":
            print("Available CommandEnum names:")
            print(", ".join(sorted(names)))
            print("Shortcuts: status, start [amps,start_date(YYYY-MM-DDTHH:MM:SS),duration(min)], stop, quit")
            # start 16 2025-08-12T12:00:00 180
            continue
        if cmd_str == "status" or cmd_str.upper() == CommandEnum.CURRENT_STATUS_EVENT.name:
            await protocol.request_status()
            continue
        if cmd_str == "start" or cmd_str.upper() == CommandEnum.CHARGE_START_REQUEST.name:
            amps = args[0] if args else 16
            if amps is not None:
                try:
                    amps = int(amps)
                except ValueError:
                    print(f"Invalid amps value({amps}), using default 16A")
                    amps = 16
            start_date = args[1] if len(args) > 1 else datetime.now()
            if start_date is not None:
                try:
                    start_date = datetime.strptime(start_date, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    print(f"Invalid start date ({start_date}), using current time")
                    start_date = datetime.now()
            duration_min = args[2] if len(args) > 2 else 65535
            if duration_min is not None:
                try:
                    duration_min = int(duration_min)
                except ValueError:
                    print(f"Invalid duration({duration_min}), using default 65535 (unlimited)")
                    duration_min = 65535
            ok = await protocol.start_charging(max_amps=amps, start_date=start_date, duration_minutes=duration_min)
            print(
                f"Start charging {'sent' if ok else 'failed'} (amps={amps}, start_date={start_date}, duration={duration_min})"
            )
            continue
        if cmd_str == "stop" or cmd_str.upper() == CommandEnum.CHARGE_STOP_REQUEST.name:
            ok = await protocol.stop_charging()
            print(f"Stop charging {'sent' if ok else 'failed'}")
            continue

        # Try to map to CommandEnum by name
        try:
            enum_cmd = CommandEnum[cmd_str.upper()]
        except KeyError:
            # Try numeric (supports 0x...)
            try:
                value = int(cmd_str, 0)
                enum_cmd = CommandEnum(value)
            except Exception:
                print("Unknown command. Type 'help' for a list of commands.")
                continue

        try:
            # For known commands that require payloads, prefer built-ins above.
            await protocol.send_packet(protocol._build_packet(enum_cmd))
            print(f"Sent command: {enum_cmd.name} (0x{int(enum_cmd):04X})")
        except Exception as e:
            print(f"Failed to send {enum_cmd}: {e}")


async def test_get_data():
    """Test EVSE data retrieval functionality."""
    if len(sys.argv) < 3:
        print("Usage: python test_data.py <host> <password>")
        sys.exit(1)

    host = sys.argv[1]
    password = sys.argv[2]

    print(f"Testing EVSE data retrieval for {host}")

    protocol = SimpleEVSEProtocol(host, password)

    status_task = None
    try:
        print("Connecting...")
        if await protocol.connect():
            print(f"Connected. Listen port: {protocol.listen_port}, Send port: {protocol.send_port}")

            print("Attempting login (includes discovery)...")
            if await protocol.login():
                print(f"Login successful! Discovered port: {protocol.send_port}")

                print("Getting status...")
                # Start background periodic status and interactive input
                status_task = asyncio.create_task(periodic_status(protocol, interval=60))
                await command_input_loop(protocol)
                # When input loop exits, stop periodic status
                if status_task:
                    status_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await status_task
            else:
                print("Login failed")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await protocol.disconnect()
        print("Disconnected")


if __name__ == "__main__":
    asyncio.run(test_get_data())
