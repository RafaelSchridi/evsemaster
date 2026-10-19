"""Stateless packet codec: everything that needs no device or socket state."""

import logging
import struct
import zoneinfo
from datetime import datetime, time

from .data_types import (
    ENVELOPE_SIZE,
    HEADER,
    TAIL,
    Action,
    ChargeRecord,
    ChargingStatus,
    CommandEnum,
    DataPacket,
    EvseDeviceInfo,
    EvseStatus,
    FaultEnum,
    WeeklyPlan,
    WeeklyPlanSlot,
    now_aware,
)

log = logging.getLogger(__name__)

ZERO_SERIAL = "0000000000000000"
SHANGHAI_TZ = zoneinfo.ZoneInfo("Asia/Shanghai")
WEEKLY_PLAN_SIZE = 64
SLOT_ON = 3
SLOT_OFF = 1


def shanghai_offset() -> int:
    """Offset between the local timezone and Shanghai timezone in seconds."""
    now = now_aware()
    local_offset = now.utcoffset().total_seconds()
    shanghai = now.replace(tzinfo=SHANGHAI_TZ).utcoffset().total_seconds()
    return int(shanghai - local_offset)


def split_frames(data: bytes) -> list[bytes]:
    """Split a datagram into frames; an EC311S at times sends a reply and the next frame in one datagram."""
    frames = []
    while True:
        declared = int.from_bytes(data[2:4]) if len(data) >= 4 else 0
        rest = data[declared:]
        if declared < ENVELOPE_SIZE or not rest.startswith(HEADER.to_bytes(2)):
            frames.append(data)
            return frames
        frames.append(data[:declared])
        data = rest


def build_packet(cmd: CommandEnum, serial: str | None, password: str | None, payload: bytes = b"") -> bytes:
    """Build a packet for the given command; serial and password may be empty for a probe."""
    packet = bytearray(ENVELOPE_SIZE + len(payload))

    # Header
    struct.pack_into(">H", packet, 0, HEADER)
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
    struct.pack_into(">H", packet, len(packet) - 2, TAIL)
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


def build_stop_charging_payload(user_id: str, line_id: int = 1) -> bytes:
    """Build the CHARGE_STOP_REQUEST payload; the charge record names user_id as who stopped the charge."""
    return struct.pack(">B16s", line_id, user_id.encode("ascii")[:16])


def parse_device_info(packet: DataPacket) -> EvseDeviceInfo | None:
    """Parse device info from a LOGIN_EVENT or LOGIN_SUCCESS_EVENT; both share this layout."""
    if packet.length() < 54:
        return None
    return EvseDeviceInfo(
        type=packet.get_int(0, 1),
        brand=packet.get_string(1, 16),
        model=packet.get_string(17, 16),
        firmware_version=packet.get_string(33, 16),
        max_power=packet.get_int(49, 4),
        max_amps=packet.get_int(53, 1),
        serial_number=packet.device_serial,
    )


def parse_faults(error_bits: int) -> list[FaultEnum]:
    """Name every fault set in the status error field, lowest number first."""
    faults = []
    for fault in FaultEnum:
        # fault 0 is the most significant bit of the 32
        if error_bits & (1 << (31 - fault)):
            faults.append(fault)
    return faults


def parse_status(packet: DataPacket) -> EvseStatus | None:
    """Parse an EVSE status payload."""
    if packet.length() < 25:
        return None
    three_phase = packet.length() >= 33
    return EvseStatus(
        line_id=packet.get_int(0, 1),
        l1_voltage=packet.read_measurement(1, 2, 10),
        l1_amps=packet.read_measurement(3, 2, 100),
        current_power=int(packet.read_measurement(5, 4)),
        total_kwh=packet.get_int(9, 4) / 100,
        inner_temperature=packet.read_temperature(13),
        outer_temperature=packet.read_temperature(15),
        emergency_stop=packet.get_int(17, 1) != 1,
        plug_state=packet.get_int(18, 1),
        output_state=packet.get_int(19, 1),
        current_state=packet.get_int(20, 1),
        faults=parse_faults(packet.get_int(21, 4)),
        l2_voltage=packet.read_measurement(25, 2, 10) if three_phase else 0.0,
        l2_amps=packet.read_measurement(27, 2, 100) if three_phase else 0.0,
        l3_voltage=packet.read_measurement(29, 2, 10) if three_phase else 0.0,
        l3_amps=packet.read_measurement(31, 2, 100) if three_phase else 0.0,
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
        max_energy_kwh=None if packet.get_int(22, 2) == 65535 else packet.get_int(22, 2) / 100,
        charge_param3=None if packet.get_int(24, 2) == 65535 else packet.get_int(24, 2) / 100,
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


def parse_charge_record(packet: DataPacket, offset: int, epoch_to_datetime) -> ChargeRecord | None:
    """Parse a charge record at offset: 0 in 0x0009/0x000A, 9 in a 0x000C history reply."""
    if packet.length() < offset + 88:
        return None

    def moment(field: int) -> datetime | None:
        raw = packet.get_int(offset + field, 4)
        return None if raw in (0, 0xFFFFFFFF) else epoch_to_datetime(raw)

    max_minutes = packet.get_int(offset + 52, 2)
    max_energy = packet.get_int(offset + 54, 2)
    return ChargeRecord(
        line_id=packet.get_int(offset, 1),
        started_by=packet.get_string(offset + 1, 16),
        stopped_by=packet.get_string(offset + 17, 16),
        charge_id=packet.get_string(offset + 33, 16),
        has_reservation=packet.get_int(offset + 49, 1) == 1,
        start_type=packet.get_int(offset + 50, 1),
        charge_type=packet.get_int(offset + 51, 1),
        max_duration_minutes=None if max_minutes in (0, 65535) else max_minutes,
        max_energy_kwh=None if max_energy == 65535 else max_energy / 100,
        stop_reason=packet.get_int(offset + 58, 1),
        reservation_datetime=moment(60),
        start_datetime=moment(64),
        stop_datetime=moment(68),
        duration_seconds=packet.get_int(offset + 72, 4),
        start_kwh_counter=packet.get_int(offset + 76, 4) / 100,
        stop_kwh_counter=packet.get_int(offset + 80, 4) / 100,
        charge_kwh=packet.get_int(offset + 84, 4) / 100,
    )


def build_record_ack(packet: DataPacket) -> bytes:
    """Acknowledge a 0x0009/0x000A record by its charge id; byte 0 is the constant 1 the app sends."""
    return bytes([1]) + packet.get_buffer(33, 16) + bytes([1])


def parse_weekly_plan(packet: DataPacket) -> WeeklyPlan | None:
    """Parse a 0x010E reply; like the app, any slot state but 1 counts as on."""
    if packet.length() < WEEKLY_PLAN_SIZE:
        return None
    slots = []
    for day in range(7):
        base = 1 + 9 * day
        slots.append(
            WeeklyPlanSlot(
                enabled=packet.get_int(base, 1) != SLOT_OFF,
                start=time(packet.get_int(base + 1, 1), packet.get_int(base + 2, 1)),
                duration_minutes=packet.get_int(base + 3, 2),
            )
        )
    return WeeklyPlan(slots=slots)


def build_weekly_plan_payload(plan: WeeklyPlan) -> bytes:
    """Build a 0x810E set; every slot is written, with the unlimited limit the app sends."""
    payload = bytearray([Action.SET])
    for slot in plan.slots:
        state = SLOT_ON if slot.enabled else SLOT_OFF
        payload += struct.pack(">BBBHI", state, slot.start.hour, slot.start.minute, slot.duration_minutes, 0xFFFFFFFF)
    return bytes(payload)
