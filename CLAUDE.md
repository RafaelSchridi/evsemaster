# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`evsemaster` is an asyncio Python (>=3.13) client library for EVSE chargers that use the EVSEMaster app, speaking its reverse-engineered UDP protocol (based on the TypeScript project [johnwoo-nl/emproto](https://github.com/johnwoo-nl/emproto)). The Home Assistant integration that consumes this library lives in the sibling repo `RafaelSchridi/evsemaster-homeassistant` (see its CLAUDE.md for the cross-repo dev workflow).

## Commands

- Install: `poetry install`
- Lint/format: `poetry run ruff check` / `poetry run ruff format` (line-length 120)
- There are no unit tests; testing happens against real hardware with the interactive script:
  `poetry run python test.py <host> <6-digit-password>` — accepts shortcuts (`status`, `start [amps] [YYYY-MM-DDTHH:MM:SS] [duration_min]`, `stop`), `CommandEnum` names, or raw hex/decimal command values.

## Architecture

Two modules in `evsemaster/`:

**`data_types.py`**
- `CommandEnum` — protocol command codes. Naming convention: `*_REQUEST` = client-initiated (you send), `*_EVENT` = anything incoming from the EVSE (reply or unsolicited), `*_RESPONSE` = what you send back to an event.
- Pydantic models `EvseDeviceInfo`, `EvseStatus`, `ChargingStatus` hold parsed device state.
- `DataPacket` — parser for incoming packets. Big-endian; header `0x0601`, device serial at bytes 5–13, command at 19–21, payload from 21.

**`evse_protocol.py`** — `SimpleEVSEProtocol`, the single public entry point.
- UDP via asyncio `DatagramProtocol`: listens on 28376, initially sends to 7248; the real device port is discovered from the source address of incoming datagrams (`_on_datagram`).
- Login: `LOGIN_REQUEST` is sent twice 5s apart (the first doubles as port discovery), then await `LOGIN_SUCCESS_EVENT` and reply `LOGIN_CONFIRM_RESPONSE`. Every outgoing packet embeds the 6-char password (`_build_packet`).
- Push-based: the EVSE sends status/charging events unsolicited; `_on_datagram` dispatches them and answers keepalives (`HEADING_EVENT`). Consumers receive updates via the `event_callback(event_type, data)` constructor arg, where `event_type` is the model class name (e.g. `"EvseStatus"`).

**Timezone/clock handling (biggest gotcha)**
- The EVSE interprets all timestamps as Asia/Shanghai local time; convert with `_datetime_to_shanghai_epoch` / `_shanghai_epoch_to_datetime`.
- Firmware bug: the device clock can drift by weeks. `_time_delta` is computed from `SYSTEM_TIME_EVENT` responses when skew > 1 day and applied when scheduling charge sessions.
- Use `now_aware()` from `data_types`, never naive `datetime.now()` — `start_charging` rejects naive datetimes.

## Releasing

Bump `version` in `pyproject.toml`. The HA integration pins the exact version in its `manifest.json` requirements (`evsemaster==x.y.z`), so update that alongside a release.
