"""Routing: which device does an incoming packet belong to."""

import asyncio
import struct

import pytest

from evsemaster import CommandEnum, EvseListener
from evsemaster.listener import DeviceAlreadyRegistered
from evsemaster.protocol import build_packet
from evsemaster.testing import device_info_payload, status_payload

SERIAL_A = "aa" * 8
SERIAL_B = "bb" * 8
STRANGER = "cc" * 8


class _StubTransport:
    """Stands in for the socket so routing can be tested without the network."""

    def __init__(self):
        self.sent: list[tuple[tuple[str, int], str, str]] = []

    def sendto(self, data, addr):
        self.sent.append((addr, CommandEnum(struct.unpack(">H", data[19:21])[0]).name, data[5:13].hex()))

    def close(self):
        pass


@pytest.fixture
def discovered():
    return []


@pytest.fixture
async def listener(discovered):
    listener = EvseListener(on_discovery=discovered.append)
    listener._transport = _StubTransport()
    yield listener
    await listener.stop()


def feed(listener, cmd, serial, addr, payload=b""):
    """Deliver a packet as if it arrived on the socket."""
    listener._on_datagram(build_packet(cmd, serial, "", payload), addr)


async def test_a_packet_is_routed_by_ip_until_the_serial_is_known(listener):
    device = await listener.async_add_device("127.0.0.1", "123456")
    assert device.serial is None

    feed(listener, CommandEnum.LOGIN_SUCCESS_EVENT, SERIAL_A, ("127.0.0.1", 46540), device_info_payload())
    await asyncio.sleep(0)

    assert device.serial == SERIAL_A
    assert listener.get_device(SERIAL_A) is device
    assert device.send_port == 46540, "the port must be learned, it differs per model"
    assert listener._transport.sent[-1] == (("127.0.0.1", 46540), "LOGIN_CONFIRM_RESPONSE", SERIAL_A)


async def test_two_chargers_do_not_receive_each_others_packets(listener):
    a = await listener.async_add_device("127.0.0.1", "111111")
    b = await listener.async_add_device("127.0.0.2", "222222")

    feed(listener, CommandEnum.LOGIN_SUCCESS_EVENT, SERIAL_A, ("127.0.0.1", 7248), device_info_payload())
    feed(listener, CommandEnum.CURRENT_STATUS_EVENT, SERIAL_B, ("127.0.0.2", 7248), status_payload())
    await asyncio.sleep(0)

    assert a.get_latest_status() is None
    assert b.get_latest_status() is not None
    assert b.get_latest_device_info() is None
    assert {addr for addr, _, _ in listener._transport.sent} == {("127.0.0.1", 7248), ("127.0.0.2", 7248)}


async def test_a_charger_that_changed_ip_is_still_found(listener):
    """Serial first, so DHCP moving a charger does not make us deaf to it."""
    device = await listener.async_add_device("127.0.0.1", "123456")
    feed(listener, CommandEnum.LOGIN_SUCCESS_EVENT, SERIAL_A, ("127.0.0.1", 7248), device_info_payload())
    await asyncio.sleep(0)

    feed(listener, CommandEnum.HEADING_EVENT, SERIAL_A, ("10.0.0.9", 7248))
    await asyncio.sleep(0)

    assert device.host_ip == "10.0.0.9"
    assert listener._by_ip.get("10.0.0.9") is device and "127.0.0.1" not in listener._by_ip
    assert listener._transport.sent[-1] == (("10.0.0.9", 7248), "HEADING_RESPONSE", SERIAL_A)


async def test_a_different_charger_at_a_known_ip_is_not_mistaken_for_it(listener, discovered):
    device = await listener.async_add_device("127.0.0.1", "123456")
    feed(listener, CommandEnum.LOGIN_SUCCESS_EVENT, SERIAL_A, ("127.0.0.1", 7248), device_info_payload())
    await asyncio.sleep(0)

    feed(listener, CommandEnum.CURRENT_STATUS_EVENT, STRANGER, ("127.0.0.1", 7248), status_payload())
    await asyncio.sleep(0)

    assert device.serial == SERIAL_A
    assert device.get_latest_status() is None
    assert [d.serial_number for d in discovered] == [STRANGER]


async def test_an_unknown_charger_is_reported_once_with_its_details(listener, discovered):
    for _ in range(3):
        feed(listener, CommandEnum.LOGIN_EVENT, STRANGER, ("10.0.0.5", 7248), device_info_payload())
    await asyncio.sleep(0)

    assert len(discovered) == 1
    found = discovered[0]
    assert (found.serial_number, found.host, found.port) == (STRANGER, "10.0.0.5", 7248)
    assert (found.brand, found.model, found.max_amps) == ("BESEN", "BS20", 32)


async def test_our_own_broadcast_probe_is_not_a_discovery(listener, discovered):
    """A probe carries no serial, so it would otherwise discover itself."""
    feed(listener, CommandEnum.LOGIN_REQUEST, None, ("192.168.1.10", 28376))
    await asyncio.sleep(0)
    assert discovered == []


async def test_a_removed_charger_can_be_discovered_again(listener, discovered):
    device = await listener.async_add_device("127.0.0.1", "123456")
    feed(listener, CommandEnum.LOGIN_SUCCESS_EVENT, SERIAL_A, ("127.0.0.1", 7248), device_info_payload())
    await asyncio.sleep(0)
    assert discovered == []

    await listener.async_remove_device(device)
    feed(listener, CommandEnum.LOGIN_EVENT, SERIAL_A, ("127.0.0.1", 7248), device_info_payload())
    await asyncio.sleep(0)

    assert [d.serial_number for d in discovered] == [SERIAL_A]
    assert listener.devices == []


async def test_a_host_cannot_be_registered_twice(listener):
    device = await listener.async_add_device("127.0.0.1", "123456")
    with pytest.raises(DeviceAlreadyRegistered) as err:
        await listener.async_add_device("127.0.0.1", "123456")
    assert err.value.device is device


async def test_a_packet_we_cannot_parse_is_dropped_quietly(listener, discovered):
    listener._on_datagram(b"\xff\xff not an evse packet at all", ("10.0.0.5", 7248))
    listener._on_datagram(b"", ("10.0.0.5", 7248))
    await asyncio.sleep(0)
    assert discovered == []
