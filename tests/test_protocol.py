"""Packet codec and payload parsing."""

import struct
from datetime import timedelta

import pytest

from evsemaster import CommandEnum, DataPacket
from evsemaster.data_types import now_aware
from evsemaster.protocol import (
    build_packet,
    build_start_charging_payload,
    parse_charging_status,
    parse_device_info,
    parse_status,
)
from evsemaster.testing import charging_payload, device_info_payload, status_payload

SERIAL = "1122334455667788"


def packet(cmd: CommandEnum, payload: bytes = b"", serial: str = SERIAL, password: str = "123456") -> DataPacket:
    return DataPacket(build_packet(cmd, serial, password, payload))


def test_build_packet_round_trip():
    raw = build_packet(CommandEnum.LOGIN_REQUEST, SERIAL, "123456")
    assert len(raw) == 25
    assert struct.unpack(">H", raw[0:2])[0] == CommandEnum.HEADER
    assert struct.unpack(">H", raw[2:4])[0] == len(raw)
    assert raw[5:13].hex() == SERIAL
    assert raw[13:19] == b"123456"
    assert struct.unpack(">H", raw[-4:-2])[0] == sum(raw[:-4]) % 0xFFFF
    assert struct.unpack(">H", raw[-2:])[0] == CommandEnum.TAIL


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


def test_unknown_command_is_rejected_with_its_value():
    raw = bytearray(build_packet(CommandEnum.LOGIN_REQUEST, SERIAL, "123456"))
    struct.pack_into(">H", raw, 19, 0x0162)
    with pytest.raises(ValueError, match="0x0162"):
        DataPacket(bytes(raw))


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


@pytest.mark.parametrize(
    "cmd", [CommandEnum.CURRENT_CHARGING_STATUS_EVENT, CommandEnum.CURRENT_CHARGING_STATUS_EVENT_2]
)
def test_charging_status_on_both_commands(cmd):
    status = parse_charging_status(packet(cmd, charging_payload()), lambda epoch: None)
    assert status.charge_id == "2026090412"
    assert status.duration_seconds == 3600
    assert status.charge_kwh == 12.34
    assert status.max_electricity == 16
    assert status.max_duration_minutes is None  # 65535 means unlimited


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
