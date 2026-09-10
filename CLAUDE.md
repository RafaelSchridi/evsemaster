# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`evsemaster` is an asyncio Python (>=3.14) client library for EVSE chargers that use the EVSEMaster app, speaking its reverse-engineered UDP protocol (based on the TypeScript project [johnwoo-nl/emproto](https://github.com/johnwoo-nl/emproto)). The Home Assistant integration that consumes this library lives in the sibling repo `RafaelSchridi/evsemaster-homeassistant` (see its CLAUDE.md for the cross-repo dev workflow).

## Commands

- Install: `poetry install`
- Lint/format: `poetry run ruff check` / `poetry run ruff format` (line-length 120)
- Test: `poetry run pytest` (or `poe check` for lint + tests). `tests/` covers the codec and
  parsers, listener routing, and device sessions driven over loopback by `evsemaster.testing.FakeEvse`.
  Each test takes a port of its own from the `listen_port` fixture; never bind 28376 in a test.
- Testing against real hardware uses the interactive script:
  `poetry run python test.py <host>:<password> [<host>:<password> ...]` — accepts shortcuts (`status`,
  `start [amps] [YYYY-MM-DDTHH:MM:SS] [duration_min]`, `stop`, `discover`, `use <n>`, `all`),
  `CommandEnum` names, or raw hex/decimal command values.

## Architecture

Modules in `evsemaster/`:

**`data_types.py`**
- `CommandEnum` — protocol command codes. Naming convention: `*_REQUEST` = client-initiated (you send), `*_EVENT` = anything incoming from the EVSE (reply or unsolicited), `*_RESPONSE` = what you send back to an event.
- Pydantic models `EvseDeviceInfo`, `EvseStatus`, `ChargingStatus`, `DiscoveredDevice`.
- `DataPacket` — parser for incoming packets. Big-endian; header `0x0601`, device serial at bytes 5–13, command at 19–21, payload from 21 up to the trailing checksum + tail (so `length()` is the true payload length: a single-phase status is 25 bytes, three-phase 33).

**`capabilities.py`** — per-model quirks the protocol cannot be asked about, keyed on the brand/model reported at login. A deny-list: unrecognised chargers get the permissive default. Reached through `EvseDevice.capabilities`.

**`protocol.py`** — stateless codec, no device or socket state: `build_packet`, `parse_device_info`, `parse_status`, `parse_charging_status`, `shanghai_offset`.

**`listener.py`** — `EvseListener` owns the single UDP socket (binds 28376, `SO_BROADCAST` for probing) and routes packets **by device serial first, source IP second**, so several chargers share one socket and a DHCP address change is followed automatically. A packet whose serial belongs to no registered device becomes a `DiscoveredDevice` (once per serial). `probe()` broadcasts a zero-serial, zero-password `LOGIN_REQUEST` so devices answer without a real password crossing the LAN.

**`testing.py`** — `FakeEvse`, a charger that speaks enough of the protocol to drive a real client. Public so the Home Assistant integration can use it in its own tests.

**`device.py`** — `EvseDevice` is one charger's session and the main consumer API. Updates are pushed to the `on_event(event_type, data)` callback, where `event_type` is the model class name (e.g. `"EvseStatus"`).

**Protocol gotchas**
- `LOGIN_EVENT` (0x0001) is *not* an error: it is the charger periodically broadcasting its device info (same payload layout as `LOGIN_SUCCESS_EVENT` 0x0002). It arrives whether or not we are logged in, so it says nothing about state; receiving one while logged out triggers an automatic re-login.
- There is no session. Measured on a Telestar EC311S: a client that never sent `LOGIN_REQUEST` gets `NICKNAME_EVENT` and `OUTPUT_AMPERAGE_EVENT` answered, and the same requests with a wrong password get `PASSWORD_ERROR_EVENT`. Authorisation is per packet — the password sits at bytes 13-19 of *every* packet — so `login()` is a password check and a device-info fetch, not a handshake that unlocks anything.
- `is_logged_in` therefore needs *both* a successful login and recent traffic from the charger: any packet except a `LOGIN_EVENT` announcement, within `SESSION_TIMEOUT` (120s). Traffic proves the charger is alive; it cannot prove our password is right, because the Telestar *broadcasts* its headings to every host on the LAN. Only the login half does, and the config flow's password validation rests on it.
- The send port differs per unit and is always learned from the source address — never hard-code it. Observed: Telestar EC311S 21937, BS20 30139, Ocular 46540; `DEFAULT_SEND_PORT` 7248 is only the opening guess. The first `LOGIN_REQUEST` regularly goes to the wrong port and times out; the retry succeeds because an inbound broadcast has taught us the real one in the meantime, which is why the retry loop in `login()` is load-bearing.
- The Telestar sends `UPLOAD_LOCAL_CHARGE_RECORD` (0x000A) constantly and it is deliberately unhandled.
- Chargers lose heading beats: a Telestar EC311S sends one every 10.1s, yet idle over 97 minutes ~40% never arrived, with gaps up to 90.9s (charging: three 20.3s gaps in four minutes). Its other traffic never paused longer than 55.1s, hence liveness counts every packet and `SESSION_TIMEOUT` is 120s, twice that longest silence. Longer also delays noticing a charger that really went away: until then a command to it reports success but goes nowhere. A timeout that reads a live charger as dead makes `stop_charging` refuse while the car keeps drawing.
- A Telestar EC311S applies the output amperage only when a charge *starts*. Mid-charge it echoes a new value back within seconds and then ignores it: the same 16A limit measured 9.3A when set mid-session and 15.3A when the session was restarted with it, on the same car. Lowering additionally faults minutes later. A BS20 reportedly accepts changes mid-charge. There is no way to ask, so it lives in `capabilities.py` and `set_output_amperage` raises `UnsupportedOperationError` instead of sending a command whose acknowledgement means nothing.
- A pending reservation reports `CHARGING_RESERVATION` even with no car plugged in (Telestar EC311S), so `NOT_CONNECTED` means nothing is scheduled and `start_charging`'s cancel-first check still fires when re-reserving.
- Every outgoing packet carries the device serial as soon as the first inbound packet binds it; some firmware ignores zero-serial requests.
- Single-phase chargers send 25-byte status payloads with no L2/L3 block. Some devices also report status under 0x000D and charging status under 0x0006.

**Timezone/clock handling (biggest gotcha)**
- The EVSE interprets all timestamps as Asia/Shanghai local time; convert with `_datetime_to_shanghai_epoch` / `_shanghai_epoch_to_datetime` on the device.
- Firmware bug: the device clock can drift by weeks. `_time_delta` is computed per device from `SYSTEM_TIME_EVENT` responses when skew > 1 day and applied when scheduling charge sessions (exposed as the `time_delta` property).
- Use `now_aware()` from `data_types`, never naive `datetime.now()` — `start_charging` rejects naive datetimes.

## Releasing

Bump `version` in `pyproject.toml`. The HA integration pins the exact version in its `manifest.json` requirements (`evsemaster==x.y.z`), so update that alongside a release.
