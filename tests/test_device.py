"""Session behaviour of one device, against a fake EVSE on loopback."""

import asyncio
from datetime import timedelta

import pytest

from evsemaster import CommandEnum, EvseListener, NotLoggedInError, now_aware
from evsemaster.data_types import CurrentStateEnum, UnsupportedOperationError
from evsemaster.device import SESSION_TIMEOUT
from evsemaster.testing import FakeEvse

SERIAL = "aa" * 8


async def until(predicate, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        await asyncio.sleep(0.02)
        if predicate():
            return True
    return False


@pytest.fixture
async def listener(listen_port):
    listener = EvseListener(listen_port=listen_port)
    assert await listener.start()
    yield listener
    await listener.stop()


@pytest.fixture
async def evse(listen_port):
    evse = await FakeEvse(SERIAL, nickname="Garage", client_port=listen_port).start()
    yield evse
    evse.stop()


@pytest.fixture
async def device(listener, evse):
    events = []
    device = await listener.async_add_device("127.0.0.1", "123456", on_event=lambda t, d: events.append((t, d)))
    device.events = events
    # wait for the charger's announcement, which is what teaches us its port
    assert await until(lambda: device.serial is not None)
    return device


async def test_login_learns_the_serial_and_port(device, evse):
    assert await device.login()
    assert device.is_logged_in
    assert device.serial == SERIAL
    assert device.send_port == evse.port
    assert await until(lambda: evse.has_session), "the login confirmation never reached the charger"


async def test_every_packet_after_the_first_carries_the_serial(device, evse):
    """Some firmware ignores requests that do not name the device."""
    assert await device.login()
    await until(lambda: CommandEnum.NICKNAME_REQUEST in evse.received)
    assert evse.serial == SERIAL


async def test_a_wrong_password_fails_the_login_without_retrying_forever(listener, evse):
    device = await listener.async_add_device("127.0.0.1", "000000")
    assert await until(lambda: device.serial == SERIAL)  # learned from the announcement
    assert await device.login() is False
    assert not device.is_logged_in
    assert CommandEnum.LOGIN_REQUEST in evse.received


async def test_the_session_expires_when_the_charger_goes_quiet(device, evse):
    assert await device.login()
    assert device.is_logged_in

    device._last_alive = now_aware() - SESSION_TIMEOUT
    assert not device.is_logged_in, "a charger that stopped sending is not logged in"


async def test_a_broadcast_while_logged_out_triggers_a_login(device, evse):
    assert await device.login()
    evse.end_session()
    device._last_alive = None

    evse.announce()
    assert await until(lambda: device.is_logged_in), "did not log back in by itself"


async def test_a_quiet_spell_does_not_drop_the_session(device, evse):
    """An idle Telestar EC311S went up to 55s between packets; a stop landing in that gap must not be refused."""
    assert await device.login()

    device._last_alive = now_aware() - timedelta(seconds=55)
    assert device.is_logged_in, "a quiet spell is not a lost session"


async def test_broadcast_headings_alone_do_not_grant_a_session(listener, evse):
    # empty password so the announcements cannot trigger an automatic login
    device = await listener.async_add_device("127.0.0.1", "")
    assert await until(lambda: device.serial is not None)

    for _ in range(3):
        evse.broadcast_heading()
        await asyncio.sleep(0.05)

    assert device.last_alive is not None, "the headings did arrive"
    assert not device.is_logged_in, "a heading we did not authenticate for is someone else's session"


async def test_headings_keep_the_session_alive(device, evse):
    assert await device.login()
    first = device.last_alive
    assert await until(lambda: device.last_alive != first)
    assert CommandEnum.HEADING_RESPONSE in evse.received


async def test_status_traffic_keeps_the_session_alive_without_headings(device, evse):
    """Idle, the Telestar loses heading beats for up to 90s while its status packets keep coming."""
    assert await device.login()
    evse.end_session()  # stops the headings
    stale = now_aware() - timedelta(seconds=50)
    device._last_alive = stale

    await device.request_status()
    assert await until(lambda: device.last_alive != stale)
    assert device.is_logged_in


async def test_announcements_are_not_signs_of_life(listener, evse):
    # no password, so the announcements cannot trigger an automatic login
    device = await listener.async_add_device("127.0.0.1", "")
    assert await until(lambda: device.serial is not None), "an announcement arrived"
    assert device.last_alive is None


async def test_status_and_charging_status_reach_the_consumer(device, evse):
    assert await device.login()
    await device.request_status()
    assert await until(lambda: device.get_latest_status() and device.get_latest_charging_status())
    assert {name for name, _ in device.events} >= {"EvseStatus", "ChargingStatus", "EvseDeviceInfo"}
    assert device.get_latest_device_info().nickname == "Garage"


@pytest.mark.parametrize("command", ["request_status", "start_charging", "stop_charging", "set_nickname"])
async def test_commands_are_refused_while_logged_out(listener, evse, command):
    # no password, so it never logs itself in when the charger announces
    device = await listener.async_add_device("127.0.0.1", "")
    assert not device.is_logged_in
    args = ("Shed",) if command == "set_nickname" else ()
    with pytest.raises(NotLoggedInError):
        await getattr(device, command)(*args)


async def test_start_charging_validates_against_the_device_limits(device, evse):
    assert await device.login()
    with pytest.raises(ValueError, match="between 6 and 32"):
        await device.start_charging(max_amps=40)
    with pytest.raises(ValueError, match="timezone aware"):
        await device.start_charging(start_date=__import__("datetime").datetime(2030, 1, 1))
    assert await device.start_charging(max_amps=16)
    assert await until(lambda: CommandEnum.CHARGE_START_REQUEST in evse.received)


async def test_setting_the_amperage_is_reflected_back(device, evse):
    assert await device.login()
    assert await device.set_output_amperage(10)
    assert await until(lambda: device.get_latest_device_info().configured_max_amps == 10)
    assert evse.amps_set == [10]


async def test_a_single_phase_charger_reports_status(listener):
    evse = await FakeEvse("bb" * 8, ip="127.0.0.2", three_phase=False, client_port=listener.listen_port).start()
    try:
        device = await listener.async_add_device("127.0.0.2", "123456")
        assert await device.login()
        await device.request_status()
        assert await until(lambda: device.get_latest_status() is not None)
        status = device.get_latest_status()
        assert status.l1_voltage == 230.1
        assert (status.l2_voltage, status.l3_voltage) == (0.0, 0.0)
    finally:
        evse.stop()


CHARGING = int(CurrentStateEnum.CHARGING)
TELESTAR = {"brand": "TELESTAR", "model": "EC311S & EC311S6"}


async def logged_in(listener):
    device = await listener.async_add_device("127.0.0.1", "123456")
    assert await until(lambda: device.serial is not None)
    assert await device.login()
    return device


@pytest.mark.parametrize("amperage", [10, 20], ids=["lower", "raise"])
async def test_a_telestar_refuses_an_amperage_change_while_charging(listener, listen_port, amperage):
    """It applies the amperage only at session start, but ACKs a mid-charge change anyway.

    Lowering faults minutes later; raising is silently ignored. Either way the command must not
    go out, because the reply would make it look like it worked.
    """
    evse = await FakeEvse(SERIAL, client_port=listen_port, state=CHARGING, **TELESTAR).start()
    try:
        device = await logged_in(listener)
        assert await until(lambda: device.is_charging)

        with pytest.raises(UnsupportedOperationError):
            await device.set_output_amperage(amperage)
        assert amperage not in evse.amps_set, "the useless command still went out"
    finally:
        evse.stop()


async def test_a_telestar_allows_an_amperage_change_while_idle(listener, listen_port):
    """Set before the session starts, the amperage is honoured."""
    evse = await FakeEvse(SERIAL, client_port=listen_port, **TELESTAR).start()
    try:
        device = await logged_in(listener)
        assert not device.is_charging

        assert await device.set_output_amperage(10)
        assert await until(lambda: 10 in evse.amps_set)
    finally:
        evse.stop()


async def test_a_bs20_allows_an_amperage_change_while_charging(listener, listen_port):
    """The quirk is per model, so an unlisted charger keeps the permissive default."""
    evse = await FakeEvse(SERIAL, client_port=listen_port, state=CHARGING).start()
    try:
        device = await logged_in(listener)
        assert await until(lambda: device.is_charging)

        assert await device.set_output_amperage(10)
        assert await until(lambda: 10 in evse.amps_set)
    finally:
        evse.stop()
