# CLAUDE.md

Guidance for Claude Code working in this repository.

## Overview

`evsemaster` is an asyncio Python (>=3.14) client library for EVSE chargers that use the
EVSEMaster app, speaking its reverse-engineered UDP protocol (after the TypeScript project
[johnwoo-nl/emproto](https://github.com/johnwoo-nl/emproto)). The Home Assistant integration
that consumes it lives in the sibling repo `RafaelSchridi/evsemaster-homeassistant` — see its
CLAUDE.md for the cross-repo workflow.

Two companion docs carry detail this file deliberately does not repeat:

- `docs/device-behaviour.md` — what a charger does on the wire, with measurements. **Read it
  before touching session or protocol code.**
- `TODO.md` — ranked open work, including known bugs and an unfixed credential leak.

## Commands

- Install: `poetry install`
- Lint/format: `poetry run ruff check` / `poetry run ruff format` (line-length 120)
- Test: `poetry run pytest`, or `poe check` for lint + tests
- Real hardware: `poetry run python test.py <host>:<password> [...]` — an interactive shell
  accepting shortcuts (`status`, `start`, `stop`, `discover`, `use <n>`, `all`), `CommandEnum`
  names, or raw command values

`tests/` covers the codec and parsers, listener routing, and device sessions driven over
loopback by `evsemaster.testing.FakeEvse`. Each test takes its own port from the `listen_port`
fixture; never bind 28376 in a test.

## Architecture

- **`data_types.py`** — `CommandEnum`, the Pydantic models, and `DataPacket` (incoming-packet
  parser, big-endian).
- **`protocol.py`** — stateless codec, no device or socket state.
- **`listener.py`** — `EvseListener` owns the single UDP socket (binds 28376) and routes
  packets by device serial first, source IP second, so several chargers share one socket and a
  DHCP change is followed automatically. Unknown serials become a `DiscoveredDevice`. `probe()`
  broadcasts a zero-serial, zero-password login so devices answer without a password crossing
  the LAN.
- **`device.py`** — `EvseDevice`, one charger's session and the main consumer API. Updates are
  pushed to `on_event(event_type, data)`, where `event_type` is the model class name.
- **`capabilities.py`** — per-model quirks the protocol cannot be asked about, keyed on the
  brand/model reported at login. A deny-list: unrecognised chargers get the permissive default.
- **`testing.py`** — `FakeEvse`. Public so the HA integration can use it in its own tests.

## Conventions and traps

- `CommandEnum` naming: `*_REQUEST` = we send, `*_EVENT` = anything incoming, `*_RESPONSE` =
  what we send back to an event.
- **Authorisation and registration are two separate things, and conflating them caused every
  stale-data bug so far.** The password is in every packet, so `is_authorised` never expires and
  commands gate on it — a charger that stopped reporting still accepts a stop. Registration is
  charger-side state tracked by `is_receiving`, judged on `HEADING_EVENT` alone within
  `HEADING_TIMEOUT`; `is_logged_in` is a deprecated alias for it. Never gate a command on liveness.
- State enums are tolerant, commands are not. `PlugStateEnum`/`CurrentStateEnum` inherit
  `FirmwareEnum`, which maps unmapped values to `UNKNOWN` rather than losing all 16 status
  fields over one byte. `CommandEnum` stays strict.
- The send port differs per unit and is always learned from the source address — never
  hard-code it. `DEFAULT_SEND_PORT` is only an opening guess, which is why the retry loop in
  `login()` is load-bearing.
- Every outgoing packet carries the device serial once an inbound packet binds it; some
  firmware ignores zero-serial requests.
- `UPLOAD_LOCAL_CHARGE_RECORD` (0x000A) arrives constantly and is deliberately unhandled.
- Per-model behaviour differences belong in `capabilities.py`, not in `if model ==` branches.

### Clocks

- The EVSE interprets all timestamps as Asia/Shanghai local time; convert with
  `_datetime_to_shanghai_epoch` / `_shanghai_epoch_to_datetime` on the device.
- The device clock drifts by weeks. `_time_delta` is computed per device from
  `SYSTEM_TIME_EVENT` when skew > 1 day and applied when scheduling.
- Use `now_aware()` from `data_types`, never naive `datetime.now()` — `start_charging` rejects
  naive datetimes.

## Releasing

Bump `version` in `pyproject.toml`. The HA integration pins the exact version in its
`manifest.json` (`evsemaster==x.y.z`), so update that alongside a release.
