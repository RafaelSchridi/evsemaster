"""Interactive test client. Usage: python test.py <host>:<password> [<host>:<password> ...]"""

import asyncio
import contextlib
import logging
import sys
from datetime import datetime

from evsemaster import CommandEnum, DiscoveredDevice, EvseDevice, EvseListener, now_aware

logging.basicConfig(level=logging.DEBUG)


def label(device: EvseDevice) -> str:
    return device.serial or device.host


def on_event(device: EvseDevice):
    def callback(event_type: str, data):
        print(f"[{label(device)}] {event_type}: {data}")

    return callback


def on_discovery(discovered: DiscoveredDevice):
    print(f"[discovery] {discovered}")


async def periodic_status(devices: list[EvseDevice], interval: int = 60):
    """Periodically request status from every logged-in EVSE."""
    try:
        while True:
            await asyncio.sleep(interval)
            for device in devices:
                if device.is_authorised:
                    await device.request_status()
    except asyncio.CancelledError:
        return


def parse_start_args(args: list[str]) -> tuple[int, datetime, int]:
    """Parse `start [amps] [YYYY-MM-DDTHH:MM:SS] [duration_min]`."""
    amps = 16
    if args:
        try:
            amps = int(args[0])
        except ValueError:
            print(f"Invalid amps value({args[0]}), using default 16A")

    start_date = now_aware()
    if len(args) > 1:
        try:
            # the EVSE rejects naive datetimes, so stamp the local zone on it
            start_date = datetime.strptime(args[1], "%Y-%m-%dT%H:%M:%S").astimezone()
        except ValueError:
            print(f"Invalid start date ({args[1]}), using current time")

    duration_min = 65535
    if len(args) > 2:
        try:
            duration_min = int(args[2])
        except ValueError:
            print(f"Invalid duration({args[2]}), using default 65535 (unlimited)")

    return amps, start_date, duration_min


async def command_input_loop(listener: EvseListener, devices: list[EvseDevice]):
    """Read commands from terminal and send them to the selected EVSE(s).

    Accepts either:
    - CommandEnum names (e.g., CURRENT_STATUS_EVENT, HEADING_EVENT)
    - Decimal or hex values (e.g., 32772 or 0x8004)
    - Shortcuts: status, start [amps] [date] [minutes], stop, discover, use <n>, all, help, quit
    """
    names = [e.name for e in CommandEnum]
    targets = list(devices)
    print("Type 'help' to list commands. Type 'quit' to exit.")
    while True:
        try:
            line = await asyncio.to_thread(input, f"evse[{','.join(label(d) for d in targets)}]> ")
        except EOFError, KeyboardInterrupt:
            print("\nExiting input loop...")
            return

        parts = line.strip().split()
        if not parts:
            continue
        cmd_str, args = parts[0].lower(), parts[1:]

        if cmd_str in ("quit", "exit"):
            return
        if cmd_str == "help":
            print("Available CommandEnum names:")
            print(", ".join(sorted(names)))
            print("Shortcuts: status, start [amps] [YYYY-MM-DDTHH:MM:SS] [minutes], stop, discover")
            print(f"Targets: use <1-{len(devices)}>, all")
            for i, device in enumerate(devices, 1):
                print(f"  {i}. {device} authorised={device.is_authorised} receiving={device.is_receiving}")
            continue
        if cmd_str == "all":
            targets = list(devices)
            continue
        if cmd_str == "use":
            try:
                targets = [devices[int(args[0]) - 1]]
            except IndexError, ValueError:
                print(f"Usage: use <1-{len(devices)}>")
            continue
        if cmd_str == "discover":
            await listener.probe()
            continue

        # resolve a raw command up front so a command error is not reported per device
        enum_cmd = None
        if cmd_str not in ("status", "start", "stop"):
            try:
                enum_cmd = CommandEnum[cmd_str.upper()]
            except KeyError:
                try:
                    enum_cmd = CommandEnum(int(cmd_str, 0))
                except ValueError:
                    print("Unknown command. Type 'help' for a list of commands.")
                    continue

        for device in targets:
            try:
                if cmd_str == "status":
                    await device.request_status()
                elif cmd_str == "start":
                    amps, start_date, duration_min = parse_start_args(args)
                    ok = await device.start_charging(
                        max_amps=amps, start_date=start_date, duration_minutes=duration_min
                    )
                    print(f"[{label(device)}] start charging {'sent' if ok else 'failed'}")
                elif cmd_str == "stop":
                    ok = await device.stop_charging()
                    print(f"[{label(device)}] stop charging {'sent' if ok else 'failed'}")
                else:
                    device.send_command(enum_cmd)
                    print(f"[{label(device)}] sent {enum_cmd.name} (0x{int(enum_cmd):04X})")
            except Exception as e:
                print(f"[{label(device)}] failed: {e}")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python test.py <host>:<password> [<host>:<password> ...]")
        sys.exit(1)

    listener = EvseListener(on_discovery=on_discovery)
    if not await listener.start():
        sys.exit(1)

    devices: list[EvseDevice] = []
    status_task = None
    try:
        for arg in sys.argv[1:]:
            host, _, password = arg.partition(":")
            device = await listener.async_add_device(host, password)
            device.on_event = on_event(device)
            devices.append(device)
            print(f"Logging in to {host}...")
            if await device.login():
                print(f"Login successful: {device}")
            else:
                print(f"Login failed for {host}; leaving it registered (it may announce itself later)")

        status_task = asyncio.create_task(periodic_status(devices))
        await command_input_loop(listener, devices)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if status_task:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_task
        await listener.stop()
        print("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
