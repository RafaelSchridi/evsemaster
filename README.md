# evsemaster

Unofficial Async Python client for EVSE chargers that speak the EVSEMaster app's UDP protocol, as used by
Besen, Telestar, evseODM, Morec, Deltacom and other rebrands of the same hardware.

> **Using Home Assistant?** You probably want the
> **[EVSEMaster integration](https://github.com/RafaelSchridi/evsemaster-homeassistant)** instead.     
>  It is built on this library and installs through HACS: sensors, start/stop, scheduled sessions and automatic discovery, with no code to write.

This library is the plumbing underneath it: one UDP socket, several chargers, parsed events. It is
deliberately smaller than the original TypeScript project it is based on,
[emproto](https://github.com/johnwoo-nl/emproto) by [johnwoo-nl](https://github.com/johnwoo-nl).

## Compatible devices

Confirmed by users of the Home Assistant integration:

- Telestar EC311S & EC311S6
- [Besen B20](https://github.com/RafaelSchridi/evsemaster-homeassistant/issues/1)
- [Morec MC20CAPP / MC20AAPP](https://github.com/RafaelSchridi/evsemaster-homeassistant/issues/9)

Chargers differ in small ways the protocol cannot be asked about, so per-model quirks live in
`capabilities.py`. An unrecognised charger gets the permissive default and normally just works.
[Report a working charger here](https://github.com/RafaelSchridi/evsemaster-homeassistant/issues/new?template=working-device.yml)
to get it listed.

## Installation

Published on PyPI as [evsemaster](https://pypi.org/project/evsemaster/); requires Python 3.14 or newer.

```bash
pip install evsemaster
```

## Usage

One `EvseListener` owns the UDP socket; every charger is an `EvseDevice` added to it. Incoming packets
are routed by the device serial in the packet header, so several chargers share the one socket.

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

Updates arrive through `on_event(event_type, data)`, where `event_type` is the model name:
`"EvseStatus"`, `"ChargingStatus"` or `"EvseDeviceInfo"`.

### Scheduling a session

```python
from datetime import timedelta
from evsemaster import now_aware

await evse.start_charging(
    max_amps=16,
    start_date=now_aware() + timedelta(hours=2),  # must be timezone aware
    duration_minutes=180,
)
```

Chargers interpret every timestamp as Asia/Shanghai local time, and some firmware has a clock that
drifts by weeks. The library converts and compensates for both, so pass normal aware datetimes and
read `evse.time_delta` if you want to see the skew it corrected.

## Networking

Chargers broadcast their presence on UDP port 28376, which is how the library learns a charger's send
port, follows it across DHCP changes and logs back in after a session drops. Those broadcasts have to
reach your host: a separate VLAN or Docker bridge networking blocks them.

`listener.probe()` broadcasts a password-less login request for chargers that stay quiet, so discovery
works without a real password crossing the network.

A device counts as logged in while the charger keeps talking to it: `evse.is_logged_in` needs a
successful login plus a packet other than an announcement within the last 120 seconds
(`evse.last_alive`).

## Implemented

- Multiple chargers on one UDP socket, routed by device serial
- Discovery of chargers on the network, including ones that must be asked
- Device info, status and charging status as parsed pydantic models
- Start/stop charging, including reservations with a start time and a duration
- Get/set nickname
- Get/set current limit, refused on models that silently ignore it mid-charge
- Device time, with correction for on-device clock drift

## Not planned

- Home Assistant or MQTT integration, see [evsemaster-homeassistant](https://github.com/RafaelSchridi/evsemaster-homeassistant)
- Recurring charge schedules; single reservations are covered by `start_charging`
- Bluetooth setup (getting the charger onto wifi). The EVSEMaster app is unpleasant, but you only need
  it once.

## Development

```bash
poetry install
poetry run poe check      # ruff check and pytest
```

`evsemaster.testing.FakeEvse` is a charger that speaks enough of the protocol to drive a real client
over loopback. It is public API, so downstream projects can use it in their own tests.

For poking at real hardware there is an interactive script that takes one or more chargers:

```bash
poetry run python test.py 10.0.0.1:123456 10.0.0.2:654321
```

Shortcuts: `status`, `start [amps] [YYYY-MM-DDTHH:MM:SS] [minutes]`, `stop`, `discover`, `use <n>`, `all`.

## Credits

The protocol was reverse engineered by **[@johnwoo-nl](https://github.com/johnwoo-nl)** in
**[emproto](https://github.com/johnwoo-nl/emproto)**. Packet structures, command codes and the
communication patterns all come from that work; this library is a Python take on it.
