# Protocol

Wire format of EVSEMaster chargers. For how chargers behave over time, see `device-behaviour.md`.

Sources, as used in the **Source** column:
- **wire**: seen in captures, mostly of a Telestar EC311S.
- **app**: decompiled from the EVSEMaster Android app 3.1.38 (`com.evsemaster.dev`, Flutter, read with blutter).
- **emproto**: [johnwoo-nl/emproto](https://github.com/johnwoo-nl/emproto).

## Transport

- UDP. The client binds port **28376**.
- The charger sends everything as a **broadcast to 255.255.255.255:28376**, from its own port. That port differs per
  unit: 21937, 30139 and 46540 have been seen.
- The client sends unicast to the charger's IP and port. Learn both from any packet the charger sends.
- Replies always go to port 28376, whatever port the request came from.
- **One datagram can hold several frames**, usually a reply with a push or heading behind it. Split by each frame's
  declared length. No frame has been seen spanning two datagrams.
- Drop a frame with a bad tail or checksum, as the vendor app does.
- **An all-`FF` frame** (command `0xFFFF`, 12-byte payload) at times arrives where a reply was due: once for `0x800D`
  in the post-login burst, once during a history read. Drop it.

## Frame

All integers are big-endian.

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | header `0x0601` |
| 2 | 2 | frame length, whole frame |
| 4 | 1 | key type, `0x00` |
| 5 | 8 | charger serial; zeros in a probe |
| 13 | 6 | password, 6 ASCII digits; `FF`×6 in frames from the charger |
| 19 | 2 | command |
| 21 | n | payload |
| 21+n | 2 | checksum = sum of all preceding bytes % `0xFFFF` |
| 23+n | 2 | tail `0x0F02` |

A frame is 25 + n bytes. **All offsets below are payload offsets** (frame offset − 21).

## Conventions

- `0x8xxx` goes from client to charger, `0x0xxx` from charger to client. A request `0x8xxx` gets the reply `0x0xxx`.
- Set/get commands start with an action byte: `1` = set, `2` = get. The reply echoes it.
- Strings are fixed width and zero padded. The app encodes and decodes every one as UTF-8; the chargers seen so far
  send only ASCII.
- Times are u32 epoch seconds calculated as if local time were Asia/Shanghai. The charger's clock drifts.
- Line id is a connector number, always `1` so far.
- The password is checked on every packet. Commands work without logging in.

## Session

1. Client sends `0x8002` login, empty payload.
2. Charger answers `0x0002` with device info, or `0x0155` if the password is wrong.
3. Client sends `0x8001` to confirm.
4. The charger now reports: `0x0003` every 10 s, `0x0004` every 5–15 s, and `0x0005` charging status.
5. Answer every `0x0003` with `0x8003`, or the charger drops the registration.

After the handshake the app sends ten commands, 50 ms apart, in this order: set system time, get status (`0x800D`),
get charging status (`0x8006`), charge fee, service fee, maneuver, language, output amps, version, weekly plan.

## Commands

`A → B` means A is sent first and B is the answer.

| Codes | Purpose | Source |
|---|---|---|
| `0x0001` | announcement with device info; only while no client is registered | wire |
| `0x8002 → 0x0002 → 0x8001` | login: request, device info, confirm | wire |
| `0x0155` | wrong password | wire |
| `0x0003 → 0x8003` | heading (keepalive) | wire |
| `0x0004 → 0x8004` | status push | wire |
| `0x800D → 0x000D` | status on demand, empty request | wire, app |
| `0x0005 → 0x8005` | charging status push; the acknowledgement is from the app | wire, app |
| `0x8006 → 0x0006` | charging status on demand, empty request | wire, app |
| `0x8007 → 0x0007` | start charge | wire |
| `0x8008 → 0x0008` | stop charge | wire |
| `0x0009 → 0x8009` | current charge record | emproto, app |
| `0x000A → 0x800A` | finished charge record, resent until acknowledged | wire, app |
| `0x800B → 0x000B` | history: capacity and counters | wire, app |
| `0x800C → 0x000C` | history: one record | wire, app |
| `0x8101 → 0x0101` | system time | wire, app |
| `0x8102 → 0x0102` | change charger password | app |
| `0x8104 → 0x0104`, `0x8105 → 0x0105` | charge fee, service fee; the app writes both on every connect | wire, app, partly decoded |
| `0x8106 → 0x0106` | version | wire, app |
| `0x8107 → 0x0107` | output amps | wire, app |
| `0x8108 → 0x0108` | nickname | wire, app |
| `0x810A → 0x010A` | Wi-Fi config, **holds the passphrase** | wire, app |
| `0x810D → 0x010D` | start method (charge without the app) | wire, emproto, app |
| `0x810E → 0x010E` | weekly plan | wire, app |
| `0x810F → 0x010F` | display language | wire, emproto, app |
| `0x8112 → 0x0112` | temperature unit | wire, emproto, app |
| `0x8122 → 0x0122`, `0x8123 → 0x0123` | RFID set, get | app, not decoded |
| `0x8150`–`0x8153` → `0x0150`–`0x0153` | firmware upgrade: start, package, result, cancel | app, not decoded |
| `0x8162 → 0x0162` | "maneuver", a multi-field settings command; no answer from EC311S | app, partly decoded |

## Payloads

### Device info (`0x0001`, `0x0002`)

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | type |
| 1 | 16 | brand |
| 17 | 16 | model |
| 33 | 16 | hardware version |
| 49 | 4 | max power |
| 53 | 1 | max amps |

An EC311S sends 119 bytes. From byte 54 it carries `TELESTAR-DIGITAL` and `GmbH`, not decoded.

Bytes 33–48 are what the app's About page labels **"Firmware version"** (`25.1651.114C0933` on an EC311S). It is not
the string [Version](#version-0x8106--0x0106) returns, which the app never shows.

### Status (`0x0004`, `0x000D`)

25 bytes on single-phase chargers, 33 on three-phase. An EC311S sends 33 with L2 and L3 at `0`, not `FFFF`, when
only L1 is live.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | line id |
| 1 | 2 | L1 voltage, ×0.1 V; `FFFF` = none |
| 3 | 2 | L1 current, ×0.01 A; `FFFF` = none |
| 5 | 4 | power, W, unscaled; `FFFFFFFF` = none; covers one phase only |
| 9 | 4 | meter total, ×0.01 kWh |
| 13 | 2 | inner temperature, (raw − 20000) × 0.01 °C; `FFFF` = none |
| 15 | 2 | outer temperature, same encoding |
| 17 | 1 | emergency stop: `1` = not pressed |
| 18 | 1 | plug state |
| 19 | 1 | output state |
| 20 | 1 | current state |
| 21 | 4 | error bits, see [Errors](#errors) |
| 25 | 8 | L2 voltage, L2 current, L3 voltage, L3 current; same encoding as L1; three-phase only |

- **Plug state:** 1 disconnected, 2 connected and unlocked, 4 connected and locked. Other values occur, such as 16
  on a BS20.
- **Output state:** 1 on, 2 off. Confirmed on the wire: over a full start/stop cycle the byte only ever took those
  two values, and it tracked the contactor rather than any state field. The app's *previous state* log line is its
  own retained value, not a wire field — it reads bytes 18 and 20 there, never 19.
- **Current state:** 1–3 fault, 10 waiting for a card swipe, 11 waiting for the button, 12 not connected,
  13 ready, 14 charging, 15 completed, 17 completed with a full battery, 20 reservation pending.

### Errors

Status bytes 21–24, read as a u32.

| Fault | Meaning | Fault | Meaning |
|---|---|---|---|
| 0–2 | relay stuck, L1–L3 | 13 | not grounded |
| 3 | offline | 14 | over voltage |
| 4 | CC error | 15 | low voltage |
| 5 | CP error | 24 | no ventilation |
| 6 | emergency stop | 25 | input power error |
| 7 | over temperature, inner | 26 | mains overload |
| 8 | over temperature, outer | 27 | diode short circuit |
| 9 | unknown | 28 | RTC failure |
| 10 | leakage protection | 29 | flash failure |
| 11 | short circuit | 30 | EEPROM failure |
| 12 | over current | 31 | metering module failure |

**Fault *n* is bit `31 − n`** — that is, position *n* counting from the most significant bit. The app converts the
error bytes to a binary string and takes `indexOf("1")` as the fault number, so it reports only the lowest-numbered
fault that is set. emproto's reading of bit *n* is wrong.

The app picks the width by payload length alone (`singleACStatus`): under 25 bytes it reads bytes 21–22 as a 16-bit
string, otherwise bytes 21–24. Both number faults from the most significant bit, so faults 0–15 sit in bytes 21–22
either way. The app has no names for faults 16–23.

### Charging status (`0x0005`, `0x0006`)

At least 74 bytes; the app guards for 80 and reads a byte at offset 74 whose meaning is unknown. An EC311S always
sends exactly 74.

Every offset below from 18 onward is confirmed against the app, which reads each field as a hex string and parses
it. Offsets 0–17 are not read on this path.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | line id |
| 1 | 1 | current state |
| 2 | 16 | charge id |
| 18 | 1 | start type |
| 19 | 1 | charge type |
| 20 | 2 | max duration, minutes; `0` or `FFFF` = none |
| 22 | 2 | max energy, ×0.01 kWh; `FFFF` = none |
| 24 | 2 | charge param 3, ×0.01; `FFFF` = none |
| 26 | 4 | reservation time; see [the 31-day clock bug](#the-31-day-clock-bug) |
| 30 | 16 | user id |
| 46 | 1 | max amps |
| 47 | 4 | time the charge was set up |
| 51 | 4 | duration, s |
| 55 | 4 | meter at start, ×0.01 kWh |
| 59 | 4 | meter now, ×0.01 kWh |
| 63 | 4 | energy this charge, ×0.01 kWh |
| 67 | 4 | price, ×0.01 |
| 71 | 1 | fee type |
| 72 | 2 | fee, ×0.01 |

### Start charge (`0x8007 → 0x0007`)

Request, 47 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | line id |
| 1 | 16 | user id |
| 17 | 16 | charge id, `yyyyMMddHHmm` |
| 33 | 1 | `0` = start now, `1` = reservation |
| 34 | 4 | start time |
| 38 | 1 | start type, `1` |
| 39 | 1 | charge type, `1` |
| 40 | 2 | max duration, minutes; `FFFF` = unlimited |
| 42 | 2 | max energy; `FFFF` = unlimited |
| 44 | 2 | charge param 3, `FFFF` |
| 46 | 1 | max amps |

Reply. The values below are the app's own message texts, which match emproto exactly:

| Offset | Field | Values |
|---|---|---|
| 0 | line id | |
| 1 | reservation result | 0 started now, 1 successful, 2 reservations not supported, 3 more than 24 h ahead, 4 time earlier than now, 5 system error, 6 a reservation already exists, 7 already charging, 8 fee incorrect, 9 power incorrect, 10 time incorrect |
| 2 | start result | |
| 3 | error reason | 0 none, 1 plug not seated properly, 2 system error, 3 already charging, 4 maintenance, 5 fee incorrect, 6 power incorrect, 7 time incorrect, 8 unknown, 20 already reserved |
| 4 | max amps | |

### Stop charge (`0x8008 → 0x0008`)

- **Request:** 17 bytes, `[line id, user id (16)]` (app). An EC311S also accepts a bare `[line id]`, but reads 16
  bytes of user id regardless: the record gets the leftovers of the previous packet, such as the epoch from a set-time
  request followed by the tail of a user id from a start request.
- **Reply:** `[line id, result, fail reason]` (emproto).

### Charge record (`0x0009`, `0x000A`, and `0x000C` from byte 9)

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | line id |
| 1 | 16 | started by: user id, or a firmware marker (see start type) |
| 17 | 16 | stopped by: user id, or a firmware marker (see stop reason) |
| 33 | 16 | charge id |
| 49 | 1 | has reservation |
| 50 | 1 | start type, see below |
| 51 | 1 | charge type: `1`, or `2` from the app when it sets a max duration |
| 52 | 6 | charge params 1–3, u16 each |
| 58 | 1 | stop reason, see below |
| 59 | 1 | has stop charge: `1`, or `2` after a power loss |
| 60 | 4 | reservation time |
| 64 | 4 | start time |
| 68 | 4 | stop time |
| 72 | 4 | duration, s |
| 76 | 4 | meter at start, ×0.01 kWh |
| 80 | 4 | meter at stop, ×0.01 kWh |
| 84 | 4 | energy, ×0.01 kWh |
| 88 | 4 | price, ×0.01 |
| 92 | 1 | fee type |
| 93 | 2 | fee, ×0.01 |
| 95 | … | power and fee logs (emproto) |

Start types and stop reasons below are mapped from the 238 records on an EC311S, from the wire alone. The charger's
duration and energy limits apply whatever the charge type: `Fix Time` ends charges of both types.

| Start type | Started by | Meaning |
|---|---|---|
| 1 | user id | a client start |
| 3 | `PAP`, charge id `PAP <epoch>` | probably plug-and-play: a start by plugging in |
| 4 | `Clock`, charge id `Clock <epoch>` | the charger's own schedule, see [Weekly plan](#weekly-plan-0x810e--0x010e) |
| 0 | `0x01` | seen once, on a blank record: charge type 0, no limits, no charge id, stop reason 14 |

| Stop reason | Stopped by | Meaning |
|---|---|---|
| 0 | user id | a reservation cancelled before it started; stop time before the reservation time, no energy |
| 11 | user id | stopped by a client |
| 14 | `Auto Stop` | unknown; seen once, on the blank record above |
| 15 | `Fix Energy` | max energy reached; seen only with a limit of 0, which ends the charge at once |
| 16 | `Fix Time` | max duration reached |
| 17 | `Pull Plug` | cable unplugged |
| 255 | `Power Down` | power loss |

`0x0009` arrives within a second of a session ending, including a reservation cancelled by a stop or by unplugging.
A record nobody acknowledges joins the `0x000A` replay. A charge started by the weekly plan records charge type 2 and
the window's length as its max duration.

**Acknowledgement** `0x8009` / `0x800A`, 18 bytes: `[1, charge id (16), 1]`. The app writes byte 0 as a constant `1`,
not the record's line id, and acknowledges every record unconditionally. Without it, the charger resends the `0x000A`
record every few seconds.

### History (`0x800B`, `0x800C`)

The charger keeps its sessions in a ring of 300 slots on an EC311S, numbered from 1. Records are written in ascending
slot order and wrap, so the newest overwrites the oldest.

- **Count:** `0x800B [0x01]` → `0x000B`, 13 bytes: `[byte, capacity u32, a u32, b u32]`.
  - The app reads only bytes 1–4 and treats them as the record count, but they hold the capacity: 300.
  - `a + b` is always the capacity, and `b` is the number of records not yet acknowledged: acknowledging the whole
    replay backlog took it from 69 to 0.
  - Byte 0 read `00`, `ff` and `01` at different times. Meaning unknown.
- **One record:** `0x800C [0x01, index u32]` → `0x000C`; bytes 0–3 are the capacity, bytes 4–7 echo the index,
  byte 8 is a flag, and a [charge record](#charge-record-0x0009-0x000a-and-0x000c-from-byte-9) starts at byte 9.
  - Flag `1`: a record follows.
  - Flag `3`: no record. Index 0 and indexes above the capacity return it. Some flag-3 slots inside the ring still hold
    an old record's bytes, others zeros, and a never-written slot is all `FF`. The app has no delete command, so what
    empties a slot is unknown.
- **Pacing:** replies go missing even at a slow pace: 18 of 300 at four requests a second, 2 of 7 at one per 2.5 s.
  Retry a read that gets no reply. Four a second with retries read all 300 slots in 89 s (331 requests).

Reading history changes nothing on the charger, and neither does acknowledging a `0x000A` record: slots 1–10 still
held their records after the app acknowledged them. The app's history screen reads nothing from the charger; it lists
the records the app itself acknowledged.

### Version (`0x8106 → 0x0106`)

Empty request. Reply, 37 bytes; an EC311S returns `25.1651.114C09` for both versions and `8ae2d4bb` as the feature:

| Offset | Size | Field |
|---|---|---|
| 0 | 16 | hardware version |
| 16 | 16 | software version |
| 32 | 4 | feature; the app shows it as "firmware signature" |
| 36 | 1 | "supportNew" in the app; `0x0B` on an EC311S, so not a boolean. The app stores it and never reads it |

The app never displays either version string. The feature word is shown as "firmware signature" and sent with firmware
update checks. It has one protocol effect: with feature word `0x554ABE8E`, the app ignores the electrical values in a
`0x000D` status.

### One-value settings

Payload `[action, value]`. Get: `[2]`; emproto sends `[2, 0]`.

| Codes | Value |
|---|---|
| `0x8107 → 0x0107` output amps | 6–32 |
| `0x810F → 0x010F` language | 1 en, 2 it, 3 de, 4 fr, 5 es, 6 he, 7 pl, 8 tr, 9 zh-Hant |
| `0x8112 → 0x0112` temperature unit | 1 °C, 2 °F |

Values are confirmed from the app for language and temperature unit. An EC311S answers all three gets: `02 10`
(16 A), `02 01` (English) and `02 01` (°C).

Language: the app gets with `[2, 0]` and sets with `[1, code]`. An EC311S echoes the new value in its set reply
(`01 03`), a get right after already returns it, and its display switches language.

### Start method (`0x810D → 0x010D`)

Which methods may start a charge: the app, an RFID card, the button, or just plugging in. The app writes it as
`[action, value]`, same shape as the one-value settings, and calls it "offline charging" in code.

The write values the app uses are `0`, `1` and `2`, matching emproto's enabled / disabled / seen-while-disabled.
The charger reports a wider range on read (values up to ~0x2e seen in the app's UI code), which encodes the exact
combination of enabled start methods. That encoding is not mapped. An EC311S answers the get `[2]` with `02 01`; it
ignores an empty request.

### Nickname (`0x8108 → 0x0108`)

33 bytes: `[action, name (32)]`. Get: `[2]`. A name being set is prefixed with `ACP#`; replies carry it without.
A set reply starts with `01` when the name was taken, or `00` and the old name when it was refused.

At most 11 characters; the charger refuses a longer name. Its display shows printable ASCII only.

### System time (`0x8101 → 0x0101`)

`[action, time u32]`. Get: `[2]`. The reply carries the charger's clock.

The time sent is the local wall clock read as if it were UTC, minus 28800 (8 h). Reading is the exact inverse.

The app writes the clock after login and again after every heading response, roughly every 10 s. It never reads it
back: `0x0101` has no handler in its dispatcher, so the reply layout is emproto's.

### The 31-day clock bug

Some firmware runs about a month ahead. It reports the offset in `0x0101` and in every reservation time, and acts on
it: a schedule must be written with the offset added to start at the intended time.

Writing the clock does not correct it. The reply to a set returns the charger's skewed time, not the value just sent.

The app corrects on display only. `HomeController.getStartTime` and `getStopTime` both do:

```
if (now + 28 days < reservation)
    reservation -= 31 days
display(reservation)
```

Threshold 2419200 s, adjustment −2678400 s, both exact and hardcoded. Screens that bypass those two getters show the
raw value. The app's scheduling path sends the uncorrected time.

Observed on an EC311S in January 2026, not since.

### Weekly plan (`0x810E → 0x010E`)

- **Size:** always 64 bytes. Get: `[2]` followed by 63 zero bytes; an EC311S also answers a bare `[2]`. Set: `[1]`
  followed by 7 slots.
- **Slots:** slot *i* starts at byte `1 + 9i`. Slot 0 is Monday: the app maps its Mon–Sun list straight onto slots
  0–6.

| Offset in slot | Size | Field |
|---|---|---|
| 0 | 1 | state: `3` = on, `1` = off |
| 1 | 1 | start hour |
| 2 | 1 | start minute |
| 3 | 2 | duration, minutes, 1–1440 |
| 5 | 4 | limit; the app always sends `FFFFFFFF` |

The app switches the whole plan off by setting every slot's state to `1`. The protocol holds a separate time for each
day, even though the app's interface sets one time for all selected days. The app computes the duration from a start
and an end time: an end before the start runs into the next day, an equal one is 1440. Its parser counts any state but
`1` as on; it only ever writes `1` and `3`.

A set reply echoes the written plan, but one of two went missing on an EC311S. The app re-reads the plan 2 s after
every set.

An EC311S with the plan off returns `01 00 00 0000 00000000` for every slot: the limit reads `0` where the app writes
`FFFFFFFF`.

A start by the charger's own schedule shows in the charge record as start type 4, `Clock`, without a reservation. A
slot is a window: plugging in during it starts a charge with the rest of the window as its max duration (plugged in
at 12:28, 212 minutes, ended by `Fix Time` at 16:00). A car already plugged in starts charging when the window opens.

**A window overrides a reservation.** An EC311S accepts a reservation while a plan is on (result 1, even for a time
inside a window). When a window opened with a reservation pending for the next day, it started the plan's charge, and
once that ended the charger reported state 15, not 20: the reservation was gone, without a record of its own. Plugging in outside a window starts nothing.

### Fees (`0x8104 → 0x0104`, `0x8105 → 0x0105`)

The app writes both after every login: charge fee `[1, 1, 0, 0]` (set, fee type 1, fee 0) and service fee with an
**empty** payload. An EC311S answers both with `01 01 0000`. The service-fee reply is the previous packet's bytes: the
same over-read as the bare [stop charge](#stop-charge-0x8008--0x0008). Not decoded further.

### Change password (`0x8102 → 0x0102`)

12 bytes: the new password (6 ASCII digits) at bytes 0–5, the rest zero. The **current** password is not in the
payload; it sits in the frame header like on every packet, and that is what authorises the change. Setting a wrong
header password is rejected the same way any command is. The factory default is `123456`.

### Wi-Fi config (`0x810A → 0x010A`)

105 bytes: action at 0, network name at 1, passphrase at 33, server IP at 99, server port at 103.

The app also builds a 41-byte form, with the name at 1 and the passphrase at 17. Which form a charger expects is
unknown; the 105-byte one is what an EC311S returns.

- **Reading returns the passphrase in plaintext.** Just avoid doing this as it broadcasts it over the entire network over UDP.
- **Setting it incorrectly takes the charger off the network** until it is fixed over Bluetooth.

## Not decoded

RFID (`0x8122`, `0x8123`) and firmware upgrade (`0x8150`–`0x8153`). Fees are only partly decoded.

`0x8162` "maneuver" is an 8-byte multi-field command: the app sends `[0, a, b, 0, 0, c, 0, 0]` and reads three
fields back. It is reused for enabling/disabling the password prompt and for the LCD brightness, so the three
values select a sub-setting and its value. The exact fields are not mapped, and an EC311S does not answer it.

`0x4082` appears in the app's send routine and is probably Bluetooth-only framing, not a UDP command.

`0x8209` appears only inside the app's frame builder, where it selects an all-`FF` serial instead of a real one — a
wildcard address. No code path in the app sends it.
