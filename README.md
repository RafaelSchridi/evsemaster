# evsemaster

Python client library for communicating with a EVSE chargers that use the EVSEMaster app. I've only done my testing on a Telestar EC311S6, but it *should* work with any EVSE that uses the EVSEMaster app protocol.   
I'm intenting to keep this a simple implementation, so it does not have all the features of the original TypeScript project, but it should be sufficient for basic use cases like my Home Assistant integration.

This is based on the original TypeScript project by [johnwoo-nl](https://github.com/johnwoo-nl/emproto)

## Currently Implemented
- Multiple chargers on one UDP socket, routed by device serial
- Discovery of chargers on the network (they announce themselves; `probe()` asks the ones that don't)
- Get EVSE device info
- Get EVSE status
- Get EVSE charging status
- Start/Stop charging
- Get/Set EVSE nickname
- Get/Set Current limit
- Get/Set Device Time (Correcting for on-device errors that causes drift)

## Being Implemented
- Create/Update charging schedule
- Getting/Setting device properties like time, language, etc.

## Not Planned
- Home Assistant or MQTT integration (see [evsemaster-homeassistant](https://github.com/RafaelSchridi/evsemaster-homeassistant))
- Connecting to the EVSE via Bluetooth (ie. for connecting the EVSE to wifi)
  * Even though the EVSEMaster app is awful to use, using it once to connect the EVSE to wifi is sufficient for most use cases.

## Installation

Published on PyPI as [evsemaster](https://pypi.org/project/evsemaster/); requires Python 3.14 or newer.

```bash
pip install evsemaster
```

## Usage

One `EvseListener` owns the UDP socket; every charger is an `EvseDevice` added to it. Incoming packets
are routed by the device serial in the packet header, so several chargers can share the one socket.

```python
import asyncio
from evsemaster import EvseListener


def on_event(event_type, data):
    print(f"Event: {event_type}, Data: {data}")


def on_discovery(discovered):
    print(f"Found an unconfigured charger: {discovered}")


async def main():
    listener = EvseListener(on_discovery=on_discovery)
    await listener.start()

    evse = await listener.async_add_device(
        host="10.0.0.1",  # IP address of the EVSE
        password="123456",  # 6 digit password of the EVSE
        on_event=on_event,  # called with every parsed update
    )
    if await evse.login():
        await evse.request_status()
        await evse.start_charging()
        await evse.stop_charging()

    await listener.stop()


asyncio.run(main())
```

Chargers broadcast their presence on the listen port (28376), so the library sees a charger before it
is logged in and re-logs in by itself when a session drops. Make sure those broadcasts can reach you:
a separate VLAN or a docker bridge network will block them.

A device counts as logged in while the charger keeps talking to it: `evse.is_logged_in` needs a successful
login plus a packet other than an announcement within the last 120 seconds (`evse.last_alive`).

There is a test script `test.py` that can be used to test the library, it takes one or more chargers.
Its a bit messy as it just prints the output while accepting commands, but it can be useful for quick testing.
```bash
poetry run python test.py 10.0.0.1:123456 10.0.0.2:654321
```
Shortcuts: `status`, `start [amps] [YYYY-MM-DDTHH:MM:SS] [minutes]`, `stop`, `discover`, `use <n>`, `all`.
