# Breaking changes

What to change when upgrading, newest first. Since 2.0.0 only a major version breaks the API; the 1.x line also broke
in minor and patch releases, so those are listed too. Crossing several releases means applying each section in turn.

## 3.0.0

| 2.x | 3.0.0 |
|---|---|
| `CommandEnum.SET_ACTION`, `GET_ACTION` | `Action.SET`, `Action.GET` |
| `CommandEnum.HEADER`, `TAIL` | `evsemaster.data_types.HEADER`, `TAIL` |
| `CommandEnum.REQUEST_STATUS_RECORD` | `CommandEnum.STATUS_EVENT` |
| `CommandEnum.CURRENT_CHARGE_RECORD_REQUEST` | `CommandEnum.STATUS_REQUEST` |
| `CommandEnum.CURRENT_CHARGING_STATUS_EVENT_2` | `CommandEnum.CHARGING_STATUS_EVENT` |
| `CommandEnum.CHARGE_START_RESPONSE` | `CommandEnum.CHARGE_START_EVENT` |
| `CommandEnum.CHARGE_STOP_RESPONSE` | `CommandEnum.CHARGE_STOP_EVENT` |
| `CommandEnum.POSSIBLE_REPEATED` | `CommandEnum.WEEKLY_PLAN_EVENT` |
| `CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD` | `CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT` |
| `EvseStatus.errors` (raw bits) | `EvseStatus.faults` (`list[FaultEnum]`) |
| `EvseDeviceInfo.hardware_version`, `DiscoveredDevice.hardware_version` | `firmware_version` |

No aliases are kept. `CommandEnum.SET_ACTION` was an alias of `LOGIN_EVENT`, so code that compared against it
matched logins.

Behaviour:
- Charge records (0x0009, 0x000A) are acknowledged. The vendor app lists only records it acknowledged itself, so
  sessions this library saw first are missing from the app's history.
- `FakeEvse` answers `STATUS_REQUEST` and `CHARGING_STATUS_REQUEST` instead of 0x0004, and reports its own clock
  (`clock_skew`) instead of echoing the time it was sent.
- `DataPacket` raises `ValueError` for a frame with a bad tail or checksum, instead of parsing it.
- `set_nickname()` raises `ValueError` for a name over 11 characters or outside printable ASCII; 2.x cut it at 28.

## 2.0.3

- `is_logged_in` is deprecated and now an alias of `is_receiving`: a heading within the last 35 s. It turns `False`
  while the charger still takes commands, so gate commands on `is_authorised`.

## 2.0.0

| 1.3.x | 2.0.0 |
|---|---|
| Python 3.13 | Python 3.14 |
| `SimpleEVSEProtocol(host, password, event_callback)` | `EvseListener()`, `await listener.start()`, then `await listener.async_add_device(host, password, on_event=...)`, which returns an `EvseDevice` with the old command methods |
| `connect()`, `disconnect()` | `listener.start()`, `listener.stop()` or `listener.async_remove_device(device)` |
| `send_packet(raw_bytes)` | `send_command(cmd, payload)` |
| `from evsemaster.evse_protocol import ...` | `from evsemaster import ...` |
| `CommandEnum.NOT_LOGGED_IN_EVENT` | `CommandEnum.LOGIN_EVENT` |
| `CommandEnum.CURRENT_CHARGING_STATUS_RESPONSE` | `CommandEnum.CURRENT_CHARGING_STATUS_EVENT_2` |
| `DataPacket.data` ends with the checksum and tail | payload only, so `length()` is the payload length |

Behaviour:
- `is_logged_in` needs a heading within 30 s on top of a successful login.

## 1.3.3

- `start_charging()` raises `ValueError` for a naive `start_date`. Pass an aware one, such as `now_aware()`.

## 1.3.2

- `ChargingStatus.reservation_datetime` and `set_datetime` are `None` when there is no reservation.
- `ChargingStatus.max_duration_minutes`, `max_energy_kwh` and `charge_param3` have no default, so building a
  `ChargingStatus` yourself needs them.

## 1.3.0

- `CommandEnum.POSSIBLE_TIME` → `SYSTEM_TIME_EVENT`.
- A naive `start_date` is read as local time, not Asia/Shanghai.

## 1.2.2

- `ChargingStatus.reservation_date` → `reservation_datetime`, `start_date` → `set_datetime`.

## 1.2.1

- `CommandEnum.NICKNAME_RESPONSE` → `NICKNAME_EVENT`, `OUTPUT_AMPERAGE_REQUEST_RESPONSE` → `OUTPUT_AMPERAGE_EVENT`.
- `start_charging()` without `max_amps` charges at the configured max amps, not 16 A.

## 1.2.0

| 1.1.0 | 1.2.0 |
|---|---|
| `CommandEnum.CURRENT_CHARGE_RECORD` | `CommandEnum.CURRENT_CHARGE_RECORD_EVENT` |
| `CommandEnum.REQUEST_CHARGE_STATUS_RECORD` | `CommandEnum.CURRENT_CHARGE_RECORD_REQUEST` |
| `CommandEnum.SET_AND_GET_NICK_NAME`, `_RESPONSE` | `CommandEnum.NICKNAME_REQUEST`, `NICKNAME_RESPONSE` |
| `CommandEnum.SET_AND_GET_OUTPUT_ELECTRICITY`, `_RESPONSE` | `CommandEnum.OUTPUT_AMPERAGE_REQUEST`, `OUTPUT_AMPERAGE_REQUEST_RESPONSE` |
| `EvseStatus.timestamp`, `ChargingStatus.timestamp` | removed |

- Removed from `CommandEnum`: `CURRENT_CHARGE_RECORD_RESPONSE`, the other `SET_AND_GET_*` and `GET_VERSION` members,
  and `PASSWORD_ERROR_RESPONSE` (an alias of `PASSWORD_ERROR_EVENT`). All are back by 3.0.0, most under new names
  such as `SYSTEM_TIME_REQUEST` and `LANGUAGE_REQUEST`.
- Commands raise `NotLoggedInError` before a login and `ValueError` on bad input, and send errors propagate. Before,
  they returned `False`.
