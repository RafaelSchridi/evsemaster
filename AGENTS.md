# AGENTS.md

## Overview

`evsemaster` is an asyncio Python (>=3.14) client library for EVSE chargers that use the
EVSEMaster app, speaking its reverse-engineered UDP protocol (after the TypeScript project
[johnwoo-nl/emproto](https://github.com/johnwoo-nl/emproto)). The Home Assistant integration
that consumes it lives in the sibling repo `RafaelSchridi/evsemaster-homeassistant` — see its
AGENTS.md for the cross-repo workflow.

Three companion docs carry detail this file deliberately does not repeat:

- `docs/device-behaviour.md` — what a charger does on the wire, with measurements. **Read it
  before touching session or protocol code.**
- `docs/protocol.md` — frame, command table and payload layouts, each marked with its source
  (wire capture, vendor app, or emproto).
- `TODO.md` — ranked open work, including known bugs.

## Commands

- Install: `poetry install`
- Lint/format: `poetry run ruff check` / `poetry run ruff format` (line-length 120)
- Test: `poetry run pytest`, or `poe check` for lint + tests
- Real hardware: `poe evse`, which reads `EVSE=<host>:<password>` from the gitignored `.env`, or
  `poetry run python test.py <host>:<password> [...]`. An interactive shell accepting shortcuts
  (`status`, `start`, `stop`, `discover`, `use <n>`, `all`) or `<command> [hex payload]`, where the
  command is a `CommandEnum` name or value. A 0x81xx settings command without a payload goes out as
  a get, and the reply's payload is printed. Requests can go missing: resend before concluding a
  charger ignores one
- Wire capture: `poe capture --seconds 60 --out cap.jsonl [CMD[:hex] ...]` records every frame the
  charger broadcasts, without logging in, and can only send gets. It hears only the charger's side
  of another client's session. `poetry run python tools/decode_history.py cap.jsonl` lists the
  charge records in a capture's history replies

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
- Charge records (0x0009, 0x000A) are acknowledged as they arrive, a user decision (2026-10-16): it ends the
  replay, but the vendor app lists only records it acknowledged itself, so it never shows those sessions.
- One datagram can carry several frames; `split_frames` in the listener separates them. `DataPacket` drops a frame
  with a bad tail or checksum, as the vendor app does. Replies also go missing,
  so a settings set is followed by a get (`_reread_later`), and history reads retry.
- Per-model behaviour differences belong in `capabilities.py`, not in `if model ==` branches.

### Clocks

- The EVSE interprets all timestamps as Asia/Shanghai local time; convert with
  `_datetime_to_shanghai_epoch` / `_shanghai_epoch_to_datetime` on the device.
- Nothing corrects the device clock on its own, so a charger no client has ever set can be weeks out.
  `set_device_time` runs on every login for that reason.
- **A charger can run a month ahead and act on it**, and writing the clock does not clear it — the set reply comes
  back skewed. Seen in Jan 2026, not since. `_time_delta` measures that offset and is what made scheduling work then,
  so do not remove it. It is measured from every `0x0101`, set reply or get, and applies only above
  `CLOCK_SKEW_THRESHOLD` (1 day): every client rewrites the clock, so a smaller skew moves again before a schedule
  fires. See the 31-day clock bug in `docs/protocol.md`.
- Use `now_aware()` from `data_types`, never naive `datetime.now()` — `start_charging` rejects
  naive datetimes.

## Releasing

Bump `version` in `pyproject.toml`. The HA integration pins the exact version in its
`manifest.json` (`evsemaster==x.y.z`), so update that alongside a release.

Every breaking change goes into `docs/breaking-changes.md` under the release that makes it, as an old → new table.
Release notes link there instead of listing renames.
