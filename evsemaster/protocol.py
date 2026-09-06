"""Stateless packet codec: everything that needs no device or socket state."""

import logging
import struct
import zoneinfo
from datetime import datetime

from .data_types import (
    ChargingStatus,
    CommandEnum,
    DataPacket,
    EvseDeviceInfo,
    EvseStatus,
    now_aware,
)

log = logging.getLogger(__name__)

ZERO_SERIAL = "0000000000000000"

SHANGHAI_TZ = zoneinfo.ZoneInfo("Asia/Shanghai")


def shanghai_offset() -> int:
    """Offset between the local timezone and Shanghai timezone in seconds."""
    now = now_aware()
    local_offset = now.utcoffset().total_seconds()
    shanghai = now.replace(tzinfo=SHANGHAI_TZ).utcoffset().total_seconds()
    return int(shanghai - local_offset)


def build_packet(cmd: CommandEnum, serial: str | None, password: str | None, payload: bytes = b"") -> bytes:
    """Build a packet for the given command; serial and password may be empty for a probe."""
    packet = bytearray(25 + len(payload))

    # Header
    struct.pack_into(">H", packet, 0, CommandEnum.HEADER)
    # Length
    struct.pack_into(">H", packet, 2, len(packet))
    # Key type
    packet[4] = 0x00
    # Device serial (8 bytes)
    if serial:
        try:
            serial_bytes = bytes.fromhex(serial)[:8]
        except ValueError:
            log.debug("Ignoring non-hex device serial %r", serial)
        else:
            packet[5 : 5 + len(serial_bytes)] = serial_bytes
    # Password (6 bytes)
    if password:
        password_bytes = password.encode("ascii")[:6]
        packet[13 : 13 + len(password_bytes)] = password_bytes
    # Command
    struct.pack_into(">H", packet, 19, cmd)
    # Payload
    packet[21 : 21 + len(payload)] = payload
    # Checksum
    struct.pack_into(">H", packet, len(packet) - 4, sum(packet[:-4]) % 0xFFFF)
    # Tail
    struct.pack_into(">H", packet, len(packet) - 2, CommandEnum.TAIL)
    return bytes(packet)


def build_start_charging_payload(
    user_id: str,
    start_date: datetime,
    start_epoch: int,
    duration_minutes: int,
    max_amps: int,
) -> bytes:
    """Build the CHARGE_START_REQUEST payload.

    start_epoch is the Shanghai epoch for start_date, which needs per-device clock skew.
    """
    payload = bytearray(47)

    # Line ID (seems to be always one 1, are there any devices with multiple lines?)
    struct.pack_into(">B", payload, 0, 1)
    # User ID (16 bytes, ASCII encoded)
    struct.pack_into(">16s", payload, 1, user_id.encode("ascii")[:16])
    # Charge ID (16 bytes, ASCII encoded)
    struct.pack_into(">16s", payload, 17, start_date.strftime("%Y%m%d%H%M").encode("ascii")[:16])
    # Reservation: 0 for now, 1 if future reservation
    struct.pack_into(">B", payload, 33, 0 if now_aware() > start_date else 1)
    # Reservation date (current time in Shanghai epoch)
    struct.pack_into(">I", payload, 34, start_epoch)
    # Start type (always 1)
    struct.pack_into(">B", payload, 38, 1)
    # Charge type (always 1)
    struct.pack_into(">B", payload, 39, 1)
    # Max duration (65535 = highest possible, unlimited)
    struct.pack_into(">H", payload, 40, duration_minutes)
    # Max energy (65535 = highest possible, unlimited)
    struct.pack_into(">H", payload, 42, 65535)
    # Charge param 3 (always 65535)
    struct.pack_into(">H", payload, 44, 65535)
    # Max electricity in amps
    struct.pack_into(">B", payload, 46, max_amps)

    return bytes(payload)


def parse_device_info(packet: DataPacket) -> EvseDeviceInfo | None:
    """Parse device info from a LOGIN_EVENT or LOGIN_SUCCESS_EVENT; both share this layout."""
    if packet.length() < 54:
        return None
    return EvseDeviceInfo(
        type=packet.get_int(0, 1),
        brand=packet.get_string(1, 16),
        model=packet.get_string(17, 16),
        hardware_version=packet.get_string(33, 16),
        max_power=packet.get_int(49, 4),
        max_amps=packet.get_int(53, 1),
        serial_number=packet.device_serial,
    )


def parse_status(packet: DataPacket) -> EvseStatus | None:
    """Parse an EVSE status payload.

    Single-phase chargers send 25 bytes and omit the L2/L3 block entirely.
    """
    if packet.length() < 25:
        return None
    three_phase = packet.length() >= 33
    return EvseStatus(
        line_id=packet.get_int(0, 1),
        l1_voltage=packet.get_int(1, 2) / 10,
        l1_amps=packet.get_int(3, 2) / 100,
        current_power=packet.get_int(5, 4),
        total_kwh=packet.get_int(9, 4) / 100,
        inner_temperature=packet.read_temperature(13),
        outer_temperature=packet.read_temperature(15),
        emergency_stop=packet.get_int(17, 1),
        plug_state=packet.get_int(18, 1),
        output_state=packet.get_int(19, 1),
        current_state=packet.get_int(20, 1),
        errors=packet.get_int(21, 4),
        l2_voltage=packet.get_int(25, 2) / 10 if three_phase else 0.0,
        l2_amps=packet.get_int(27, 2) / 100 if three_phase else 0.0,
        l3_voltage=packet.get_int(29, 2) / 10 if three_phase else 0.0,
        l3_amps=packet.get_int(31, 2) / 100 if three_phase else 0.0,
    )


def parse_charging_status(packet: DataPacket, epoch_to_datetime) -> ChargingStatus | None:
    """Parse an AC charging status payload.

    epoch_to_datetime converts the device's Shanghai epochs, which needs per-device clock skew.
    """
    if packet.length() < 74:
        return None

    raw_reservation_epoch = packet.get_int(26, 4)
    raw_set_epoch = packet.get_int(47, 4)
    raw_max_duration_minutes = packet.get_int(20, 2)

    return ChargingStatus(
        line_id=packet.get_int(0, 1),
        current_state=packet.get_int(1, 1),
        charge_id=packet.get_string(2, 16),
        start_type=packet.get_int(18, 1),
        charge_type=packet.get_int(19, 1),
        max_duration_minutes=None if raw_max_duration_minutes in (0, 65535) else raw_max_duration_minutes,
        max_energy_kwh=None if packet.get_int(22, 2) == 65535 else packet.get_int(22, 2) * 0.01,
        charge_param3=None if packet.get_int(24, 2) == 65535 else packet.get_int(24, 2) * 0.01,
        reservation_datetime=epoch_to_datetime(raw_reservation_epoch) if raw_reservation_epoch else None,
        user_id=packet.get_string(30, 16),
        max_electricity=packet.get_int(46, 1),
        set_datetime=epoch_to_datetime(raw_set_epoch) if raw_set_epoch else None,
        duration_seconds=packet.get_int(51, 4),
        start_kwh_counter=packet.get_int(55, 4) / 100,
        current_kwh_counter=packet.get_int(59, 4) / 100,
        charge_kwh=packet.get_int(63, 4) / 100,
        charge_price=packet.get_int(67, 4) / 100,
        fee_type=packet.get_int(71, 1),
        charge_fee=packet.get_int(72, 2) / 100,
    )
