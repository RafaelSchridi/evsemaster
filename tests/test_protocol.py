"""Packet codec and payload parsing."""

import struct
from datetime import UTC, datetime, time, timedelta

import pytest

from evsemaster import CommandEnum, DataPacket, FaultEnum, StartTypeEnum, StopReasonEnum, WeeklyPlan, WeeklyPlanSlot
from evsemaster.data_types import HEADER, TAIL, now_aware
from evsemaster.protocol import (
    build_packet,
    build_record_ack,
    build_start_charging_payload,
    build_stop_charging_payload,
    build_weekly_plan_payload,
    parse_charge_record,
    parse_charging_status,
    parse_device_info,
    parse_status,
    parse_weekly_plan,
    split_frames,
)
from evsemaster.testing import charge_record_payload, charging_payload, device_info_payload, status_payload

SERIAL = "1122334455667788"
UNASSIGNED = 0x7777


def packet(cmd: CommandEnum, payload: bytes = b"", serial: str = SERIAL, password: str = "123456") -> DataPacket:
    return DataPacket(build_packet(cmd, serial, password, payload))


def test_build_packet_round_trip():
    raw = build_packet(CommandEnum.LOGIN_REQUEST, SERIAL, "123456")
    assert len(raw) == 25
    assert struct.unpack(">H", raw[0:2])[0] == HEADER
    assert struct.unpack(">H", raw[2:4])[0] == len(raw)
    assert raw[5:13].hex() == SERIAL
    assert raw[13:19] == b"123456"
    assert struct.unpack(">H", raw[-4:-2])[0] == sum(raw[:-4]) % 0xFFFF
    assert struct.unpack(">H", raw[-2:])[0] == TAIL


def test_build_packet_without_serial_or_password_stays_zeroed():
    """The discovery probe must not put a password on the wire."""
    raw = build_packet(CommandEnum.LOGIN_REQUEST, None, None)
    assert len(raw) == 25
    assert raw[5:19] == bytes(14)


def test_build_packet_ignores_a_serial_that_is_not_hex():
    raw = build_packet(CommandEnum.LOGIN_REQUEST, "not-hex", "123456", b"\x01")
    assert raw[5:13] == bytes(8)
    assert len(raw) == 26  # payload still intact


def test_payload_excludes_the_checksum_and_tail():
    """length() is the protocol payload length, which every parser's guards depend on."""
    assert packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(three_phase=False)).length() == 25
    assert packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(three_phase=True)).length() == 33
    assert packet(CommandEnum.HEADING_EVENT).length() == 0


def unknown_command(payload: bytes = b"") -> bytes:
    raw = bytearray(build_packet(CommandEnum.LOGIN_REQUEST, SERIAL, "123456", payload))
    struct.pack_into(">H", raw, 19, UNASSIGNED)
    return bytes(raw)


def test_unknown_command_is_rejected_with_its_value():
    with pytest.raises(ValueError, match="0x7777"):
        DataPacket(unknown_command())


def test_unknown_command_dumps_the_packet_without_the_password():
    """The dump is what a reporter pastes into an issue, so it must carry the bytes and not the password."""
    with pytest.raises(ValueError) as err:
        DataPacket(unknown_command(b"\xab\xcd"))

    assert "abcd" in str(err.value), "payload bytes missing from the dump"
    assert SERIAL in str(err.value), "serial missing from the dump"
    assert "313233343536" not in str(err.value), "password leaked into the dump"


def test_command_enum_holds_only_commands():
    """SET_ACTION = 1 and GET_ACTION = 2 were silent aliases of the two login commands."""
    assert CommandEnum(1) is CommandEnum.LOGIN_EVENT
    assert CommandEnum(2) is CommandEnum.LOGIN_SUCCESS_EVENT
    assert not {"SET_ACTION", "GET_ACTION", "HEADER", "TAIL"} & set(CommandEnum.__members__)


def test_fee_replies_are_known_commands():
    """The vendor app writes both fees on every connect, so the replies arrive whether we ask or not."""
    assert packet(CommandEnum(0x0104), bytes.fromhex("01010000")).command is CommandEnum.CHARGE_FEE_EVENT
    assert packet(CommandEnum(0x0105), bytes.fromhex("01010000")).command is CommandEnum.SERVICE_FEE_EVENT


def test_a_frame_with_a_bad_checksum_is_dropped():
    """The vendor app drops it too."""
    raw = bytearray(build_packet(CommandEnum.HEADING_EVENT, SERIAL, "123456", b"\x01"))
    raw[21] ^= 0xFF  # payload byte, so the checksum no longer covers it

    with pytest.raises(ValueError, match="checksum"):
        DataPacket(bytes(raw))


def test_a_datagram_carrying_two_frames_is_split():
    """Seen on an EC311S: a heading with a status frame behind it, and a weekly plan set reply with the get reply."""
    heading = build_packet(CommandEnum.HEADING_EVENT, SERIAL, "")
    status = build_packet(CommandEnum.CURRENT_STATUS_EVENT, SERIAL, "", status_payload())
    assert split_frames(heading + status) == [heading, status]
    assert split_frames(status) == [status]


def test_trailing_bytes_that_are_not_a_frame_stay_with_the_frame():
    heading = build_packet(CommandEnum.HEADING_EVENT, SERIAL, "")
    assert split_frames(heading + b"\x00\x01") == [heading + b"\x00\x01"]


def test_short_and_malformed_packets_are_rejected():
    with pytest.raises(ValueError):
        DataPacket(b"\x06\x01" + bytes(10))
    with pytest.raises(ValueError, match="header"):
        DataPacket(b"\xff\xff" + bytes(30))


def test_status_of_a_three_phase_charger():
    status = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(three_phase=True)))
    assert (status.l1_voltage, status.l1_amps) == (230.1, 16.0)
    assert (status.l2_voltage, status.l2_amps) == (230.2, 15.0)
    assert (status.l3_voltage, status.l3_amps) == (230.3, 14.0)
    assert (status.inner_temperature, status.outer_temperature) == (25.0, 20.0)
    assert status.current_state.name == "READY_TO_CHARGE"
    assert status.plug_state.name == "CONNECTED_LOCKED"


def test_status_of_a_single_phase_charger():
    """25 byte payload, no L2/L3 block; these used to be dropped entirely."""
    single = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(three_phase=False)))
    three = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(three_phase=True)))
    assert single is not None
    for field in ("l1_voltage", "l1_amps", "current_power", "total_kwh", "current_state", "plug_state"):
        assert getattr(single, field) == getattr(three, field)
    assert (single.l2_voltage, single.l2_amps, single.l3_voltage, single.l3_amps) == (0.0, 0.0, 0.0, 0.0)


def test_unmapped_state_values_keep_the_rest_of_the_status():
    """A BS20 reports plug state 16; strict enums used to discard all 16 fields over it."""
    status = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(plug=16, state=200)))
    assert status is not None, "the packet was discarded over one unmapped byte"
    assert status.plug_state.name == "UNKNOWN"
    assert status.current_state.name == "UNKNOWN"
    assert (status.l1_voltage, status.l1_amps) == (230.1, 16.0)


def test_emergency_stop_reads_raw_1_as_not_pressed():
    """4937 of 4937 status packets over 20.7 h read raw 1 while charging normally."""
    assert not parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload())).emergency_stop
    assert parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(emergency_stop=True))).emergency_stop


def test_measurements_without_a_reading_parse_as_zero():
    """A single-phase charger may report FFFF for L2/L3; that read as 6553.5 V and 655.35 A."""
    payload = bytearray(status_payload())
    struct.pack_into(">I", payload, 5, 0xFFFFFFFF)
    payload[25:33] = b"\xff" * 8
    status = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, bytes(payload)))
    assert status.current_power == 0
    assert (status.l2_voltage, status.l2_amps, status.l3_voltage, status.l3_amps) == (0.0, 0.0, 0.0, 0.0)
    assert (status.l1_voltage, status.l1_amps) == (230.1, 16.0)


@pytest.mark.parametrize(
    ("errors", "faults"),
    [
        (0, []),
        (1 << 31, [FaultEnum.RELAY_STUCK_L1]),
        (1 << 25, [FaultEnum.EMERGENCY_STOP]),
        (1 << 25 | 1, [FaultEnum.EMERGENCY_STOP, FaultEnum.METERING_MODULE_FAILURE]),
        (1 << 15, [FaultEnum.FAULT_16]),
    ],
)
def test_fault_n_is_bit_31_minus_n(errors, faults):
    """emproto reads bit n, which names the wrong fault; the app shows only the first of several."""
    status = parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, status_payload(errors=errors)))
    assert status.faults == faults


def test_status_too_short_to_parse_is_dropped():
    assert parse_status(packet(CommandEnum.CURRENT_STATUS_EVENT, bytes(24))) is None


@pytest.mark.parametrize("cmd", [CommandEnum.LOGIN_EVENT, CommandEnum.LOGIN_SUCCESS_EVENT])
def test_device_info_has_the_same_layout_on_both_login_commands(cmd):
    info = parse_device_info(packet(cmd, device_info_payload()))
    assert (info.brand, info.model, info.max_amps, info.max_power) == ("BESEN", "BS20", 32, 7400)
    assert info.serial_number == SERIAL


def test_device_info_too_short_to_parse_is_dropped():
    """Better no device info than max_amps read as 0."""
    assert parse_device_info(packet(CommandEnum.LOGIN_EVENT, bytes(53))) is None


@pytest.mark.parametrize("cmd", [CommandEnum.CURRENT_CHARGING_STATUS_EVENT, CommandEnum.CHARGING_STATUS_EVENT])
def test_charging_status_on_both_commands(cmd):
    status = parse_charging_status(packet(cmd, charging_payload()), lambda epoch: None)
    assert status.charge_id == "2026090412"
    assert status.duration_seconds == 3600
    assert status.charge_kwh == 12.34
    assert status.max_electricity == 16
    assert status.max_duration_minutes is None  # 65535 means unlimited


def test_charging_status_limits_scale_without_float_noise():
    """57 * 0.01 is 0.5700000000000001."""
    payload = bytearray(charging_payload())
    struct.pack_into(">HH", payload, 22, 57, 57)
    status = parse_charging_status(packet(CommandEnum.CHARGING_STATUS_EVENT, bytes(payload)), lambda epoch: None)
    assert (status.max_energy_kwh, status.charge_param3) == (0.57, 0.57)


def test_charging_status_too_short_to_parse_is_dropped():
    assert parse_charging_status(packet(CommandEnum.CURRENT_CHARGING_STATUS_EVENT, bytes(73)), lambda e: None) is None


def test_start_charging_payload_round_trip():
    start = now_aware() - timedelta(minutes=1)
    payload = build_start_charging_payload(
        user_id="evsemasterpy", start_date=start, start_epoch=1757000000, duration_minutes=90, max_amps=16
    )
    assert len(payload) == 47
    assert struct.unpack_from(">B", payload, 0)[0] == 1  # line id
    assert struct.unpack_from(">16s", payload, 1)[0].rstrip(b"\x00") == b"evsemasterpy"
    assert struct.unpack_from(">16s", payload, 17)[0].rstrip(b"\x00").decode() == start.strftime("%Y%m%d%H%M")
    assert struct.unpack_from(">I", payload, 34)[0] == 1757000000
    assert struct.unpack_from(">H", payload, 40)[0] == 90
    assert struct.unpack_from(">H", payload, 42)[0] == 65535  # max energy, unlimited
    assert struct.unpack_from(">B", payload, 46)[0] == 16


@pytest.mark.parametrize(("offset", "expected"), [(timedelta(minutes=-1), 0), (timedelta(hours=1), 1)])
def test_start_charging_payload_flags_a_future_start_as_a_reservation(offset, expected):
    payload = build_start_charging_payload(
        user_id="u", start_date=now_aware() + offset, start_epoch=0, duration_minutes=65535, max_amps=6
    )
    assert struct.unpack_from(">B", payload, 33)[0] == expected


def test_stop_charging_payload_names_the_stopper():
    """The firmware reads 16 bytes of user id whatever we send: a 1-byte stop recorded `<epoch>masterpy`."""
    assert build_stop_charging_payload("evsemasterpy") == b"\x01evsemasterpy" + bytes(4)


def epoch_as_utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


@pytest.mark.parametrize("offset", [0, 9], ids=["upload", "history"])
def test_a_charge_record_parses_at_both_offsets(offset):
    record_bytes = charge_record_payload(start=1_792_000_000, duration=5400, kwh=1234, stop_reason=16)
    header = struct.pack(">IIB", 300, 59, 1) if offset else b""
    record = parse_charge_record(packet(CommandEnum.HISTORY_RECORD_EVENT, header + record_bytes), offset, epoch_as_utc)
    assert (record.started_by, record.stopped_by, record.charge_id) == ("evsemasterpy", "evsemasterpy", "202610161200")
    assert record.start_type == StartTypeEnum.CLIENT
    assert record.stop_reason == StopReasonEnum.TIME_LIMIT
    assert record.start_datetime == epoch_as_utc(1_792_000_000)
    assert record.stop_datetime == epoch_as_utc(1_792_005_400)
    assert record.duration_seconds == 5400
    assert (record.start_kwh_counter, record.stop_kwh_counter, record.charge_kwh) == (1000.0, 1012.34, 12.34)
    assert (record.max_duration_minutes, record.max_energy_kwh) == (None, None)


def test_a_charge_record_without_times_reads_none():
    """Never-written fields are 0 or all FF; neither is a moment."""
    record_bytes = bytearray(charge_record_payload())
    struct.pack_into(">III", record_bytes, 60, 0, 0xFFFFFFFF, 0)
    record = parse_charge_record(
        packet(CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT, bytes(record_bytes)), 0, epoch_as_utc
    )
    assert (record.reservation_datetime, record.start_datetime, record.stop_datetime) == (None, None, None)


def test_record_ack_names_the_charge_id():
    ack = build_record_ack(
        packet(CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT, charge_record_payload("PAP   1792000000"))
    )
    assert ack == b"\x01PAP   1792000000\x01"


def test_weekly_plan_round_trips():
    evening = WeeklyPlanSlot(enabled=True, start=time(22, 30), duration_minutes=480)
    plan = WeeklyPlan(slots=[evening, WeeklyPlanSlot(), evening, WeeklyPlanSlot(), WeeklyPlanSlot(), evening, evening])
    payload = build_weekly_plan_payload(plan)
    assert len(payload) == 64
    assert payload[1:10] == bytes.fromhex("03 16 1e 01e0 ffffffff")
    assert payload[10:19] == bytes.fromhex("01 00 00 0000 ffffffff")
    assert parse_weekly_plan(packet(CommandEnum.WEEKLY_PLAN_EVENT, b"\x02" + payload[1:])) == plan


def test_any_slot_state_but_1_reads_as_on():
    """The app's parser skips only state 1, and so do we."""
    payload = bytes([2]) + bytes.fromhex("02 08 00 003c 00000000") + bytes.fromhex("01 00 00 0000 00000000") * 6
    plan = parse_weekly_plan(packet(CommandEnum.WEEKLY_PLAN_EVENT, payload))
    assert plan.slots[0] == WeeklyPlanSlot(enabled=True, start=time(8, 0), duration_minutes=60)
    assert not any(slot.enabled for slot in plan.slots[1:])
