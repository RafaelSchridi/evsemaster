"""Session behaviour of one device, against a fake EVSE on loopback."""

import asyncio
import struct
from datetime import time, timedelta

import pytest

from evsemaster import (
    Action,
    ChargeRecord,
    CommandEnum,
    EvseListener,
    LanguageEnum,
    NotLoggedInError,
    WeeklyPlan,
    WeeklyPlanSlot,
    now_aware,
)
from evsemaster import device as device_module
from evsemaster.data_types import CurrentStateEnum, UnsupportedOperationError
from evsemaster.device import HEADING_TIMEOUT
from evsemaster.protocol import shanghai_offset
from evsemaster.testing import FakeEvse, charge_record_payload, charging_payload, device_info_payload

SERIAL = "aa" * 8
MONTH = 31 * 86400


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
    assert device.is_receiving
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
    assert not device.is_authorised
    assert CommandEnum.LOGIN_REQUEST in evse.received


async def test_the_registration_expires_when_the_headings_stop(device, evse):
    assert await device.login()
    assert device.is_receiving

    device._last_heading = now_aware() - HEADING_TIMEOUT
    assert not device.is_receiving, "a charger that stopped heading has dropped us"
    assert device.is_authorised, "but the password is still right"


async def test_a_broadcast_while_unregistered_triggers_a_login(device, evse):
    assert await device.login()
    evse.end_session()
    device._last_heading = None
    device._last_login_attempt = None  # as if the cooldown had elapsed

    evse.announce()
    assert await until(lambda: device.is_receiving), "did not log back in by itself"


async def test_a_missed_heading_beat_does_not_drop_the_registration(device, evse):
    """Beats arrived ~100% of the time over 20.7 hours, but a stop landing in a gap must not be refused."""
    assert await device.login()

    device._last_heading = now_aware() - timedelta(seconds=20)
    assert device.is_receiving, "two missed beats is not a lost registration"


async def test_broadcast_headings_alone_do_not_grant_a_session(listener, evse):
    # empty password so the announcements cannot trigger an automatic login
    device = await listener.async_add_device("127.0.0.1", "")
    assert await until(lambda: device.serial is not None)

    for _ in range(3):
        evse.broadcast_heading()
        await asyncio.sleep(0.05)

    assert device.last_alive is not None, "the headings did arrive"
    assert not device.is_receiving, "a heading we did not authenticate for is someone else's session"


async def test_headings_hold_the_registration_open(device, evse):
    assert await device.login()
    first = device.last_heading
    assert await until(lambda: device.last_heading != first)
    assert CommandEnum.HEADING_RESPONSE in evse.received


async def test_an_answered_poll_is_not_proof_of_a_registration(device, evse):
    """A charger answers requests whether or not it still counts us as its client.

    Judging liveness on any traffic made a two hour outage look healthy: the watchdog's own
    answered poll kept refreshing the timestamp that decides whether to log in again.
    """
    assert await device.login()
    evse.end_session()
    device._last_heading = now_aware() - HEADING_TIMEOUT
    stale = now_aware() - timedelta(seconds=50)
    device._last_alive = stale

    await device.request_status()
    assert await until(lambda: device.last_alive != stale), "the charger did answer"
    assert not device.is_receiving, "an answered poll is not a registration"


async def test_a_charger_that_answers_but_stops_heading_is_registered_again(device, evse):
    """2026-09-18: a charger answered every poll for two hours while heading nobody.

    Its answers kept the old any-traffic liveness fresh, so the automatic re-login never
    fired and the integration served a two hour old snapshot as current.
    """
    assert await device.login()
    evse.end_session()
    device._last_heading = now_aware() - HEADING_TIMEOUT
    device._last_login_attempt = None  # as if the cooldown had elapsed

    await device.request_status()
    assert await until(lambda: evse.has_session), "never registered with the charger again"
    assert await until(lambda: device.is_receiving)


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


async def test_status_is_requested_with_the_commands_a_charger_answers(device, evse):
    """The EC311S ignored 0x0004, the charger's own push, so the watchdog never refreshed status."""
    assert await device.login()
    evse.received.clear()

    await device.request_status()
    assert await until(lambda: {CommandEnum.STATUS_REQUEST, CommandEnum.CHARGING_STATUS_REQUEST} <= set(evse.received))
    assert CommandEnum.CURRENT_STATUS_EVENT not in evse.received
    assert CommandEnum.CURRENT_STATUS_RESPONSE not in evse.received, "a reply is not a push, nothing to acknowledge"


async def test_a_pushed_charging_status_is_acknowledged(device, evse):
    assert await device.login()
    evse.send(CommandEnum.CURRENT_CHARGING_STATUS_EVENT, charging_payload())
    assert await until(lambda: CommandEnum.CURRENT_CHARGING_STATUS_RESPONSE in evse.received)
    assert evse.payloads[CommandEnum.CURRENT_CHARGING_STATUS_RESPONSE] == b"\x00"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("request_status", ()),
        ("start_charging", ()),
        ("stop_charging", ()),
        ("set_nickname", ("Shed",)),
        ("set_language", (LanguageEnum.GERMAN,)),
        ("set_weekly_plan", (WeeklyPlan(slots=[WeeklyPlanSlot()] * 7),)),
        ("read_history", ()),
    ],
)
async def test_commands_are_refused_until_a_login_succeeds(listener, evse, command, args):
    # no password, so it never logs itself in when the charger announces
    device = await listener.async_add_device("127.0.0.1", "")
    assert not device.is_authorised
    with pytest.raises(NotLoggedInError):
        await getattr(device, command)(*args)


@pytest.mark.parametrize("command", ["request_status", "stop_charging"])
async def test_commands_still_work_after_the_charger_drops_us(device, evse, command):
    """Authorisation is per packet, so a stop refused while the car draws is worse than stale data."""
    assert await device.login()
    evse.end_session()
    device._last_heading = now_aware() - HEADING_TIMEOUT
    assert not device.is_receiving

    assert await getattr(device, command)()


async def test_start_charging_validates_against_the_device_limits(device, evse):
    assert await device.login()
    with pytest.raises(ValueError, match="between 6 and 32"):
        await device.start_charging(max_amps=40)
    with pytest.raises(ValueError, match="timezone aware"):
        await device.start_charging(start_date=__import__("datetime").datetime(2030, 1, 1))
    assert await device.start_charging(max_amps=16)
    assert await until(lambda: CommandEnum.CHARGE_START_REQUEST in evse.received)


async def test_a_stop_names_us_as_the_stopper(device, evse):
    assert await device.login()
    assert await device.stop_charging()
    assert await until(lambda: CommandEnum.CHARGE_STOP_REQUEST in evse.payloads)
    assert evse.payloads[CommandEnum.CHARGE_STOP_REQUEST] == b"\x01evsemasterpy" + bytes(4)


async def test_setting_the_amperage_is_reflected_back(device, evse):
    assert await device.login()
    assert await device.set_output_amperage(10)
    assert await until(lambda: device.get_latest_device_info().configured_max_amps == 10)
    assert evse.amps_set == [10]


@pytest.mark.parametrize("skew", [MONTH, -MONTH], ids=["ahead", "behind"])
async def test_a_skewed_clock_is_measured_at_login(device, evse, skew):
    """The set reply at login is the only 0x0101 a pushing charger reliably sends; a get may never run."""
    evse.clock_skew = skew
    assert await device.login()
    assert await until(lambda: device.time_delta == skew)
    assert evse.payloads[CommandEnum.SYSTEM_TIME_REQUEST][0] == Action.SET, "measured from a get, not the set"


async def test_a_skew_under_a_day_is_left_alone(device, evse):
    """Drift, or another client writing the clock in its own timezone; our next write moves it again."""
    evse.clock_skew = 2 * 3600
    assert await device.login()
    assert await until(lambda: device.get_latest_device_info().nickname == "Garage")  # answered after the clock
    assert device.time_delta == 0


async def test_a_corrected_clock_drops_the_skew(device, evse):
    evse.clock_skew = MONTH
    assert await device.login()
    assert await until(lambda: device.time_delta == MONTH)

    evse.clock_skew = 0
    assert await device.set_device_time()
    assert await until(lambda: device.time_delta == 0), "a skew latched once was kept forever"


async def test_a_schedule_is_aimed_at_the_chargers_own_clock(device, evse):
    """In January 2026 a charger running a month ahead started a schedule only once the skew was added."""
    evse.clock_skew = MONTH
    assert await device.login()
    assert await until(lambda: device.time_delta == MONTH)

    start = now_aware() + timedelta(hours=2)
    assert await device.start_charging(start_date=start)
    assert await until(lambda: CommandEnum.CHARGE_START_REQUEST in evse.payloads)
    start_epoch = struct.unpack_from(">I", evse.payloads[CommandEnum.CHARGE_START_REQUEST], 34)[0]
    assert start_epoch == int(start.timestamp()) - shanghai_offset() + MONTH


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


@pytest.fixture
def fast_rereads(monkeypatch):
    monkeypatch.setattr(device_module, "SETTING_REREAD_DELAY", 0.05)
    monkeypatch.setattr(device_module, "HISTORY_REQUEST_INTERVAL", 0.01)
    monkeypatch.setattr(device_module, "HISTORY_REPLY_WAIT", 0.2)


@pytest.mark.parametrize(
    ("event", "response"),
    [
        (CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT, CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_RESPONSE),
        (CommandEnum.CURRENT_CHARGE_RECORD_EVENT, CommandEnum.CURRENT_CHARGE_RECORD_RESPONSE),
    ],
)
async def test_a_charge_record_is_handed_over_and_acknowledged(device, evse, event, response):
    """Without the acknowledgement the charger replays the record every few seconds, forever."""
    assert await device.login()
    evse.upload_record(
        charge_record_payload(charge_id="202610161200", kwh=850),
        current=event != CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT,
    )

    assert await until(lambda: response in evse.payloads)
    assert evse.payloads[response] == b"\x01202610161200\x00\x00\x00\x00\x01"
    records = [data for name, data in device.events if name == ChargeRecord.__name__]
    assert [record.charge_kwh for record in records] == [8.5]


async def test_history_asks_again_for_replies_that_went_missing(listener, listen_port, fast_rereads):
    evse = await FakeEvse(
        SERIAL,
        client_port=listen_port,
        history_capacity=5,
        history={1: charge_record_payload("a"), 2: charge_record_payload("b")},
    ).start()
    try:
        device = await listener.async_add_device("127.0.0.1", "123456")
        assert await until(lambda: device.serial is not None)
        assert await device.login()
        evse.drop_history_once = {2}

        history = await device.read_history()
        assert {slot: record.charge_id for slot, record in history.items()} == {1: "a", 2: "b"}
        assert evse.received.count(CommandEnum.HISTORY_RECORD_REQUEST) == 6, "slot 2 is asked twice, the rest once"
    finally:
        evse.stop()


async def test_history_leaves_out_empty_slots(listener, listen_port, fast_rereads):
    evse = await FakeEvse(
        SERIAL, client_port=listen_port, history_capacity=4, history={3: charge_record_payload()}
    ).start()
    try:
        device = await listener.async_add_device("127.0.0.1", "123456")
        assert await until(lambda: device.serial is not None)
        assert await device.login()
        assert list(await device.read_history()) == [3]
    finally:
        evse.stop()


async def test_a_nickname_goes_out_behind_the_acp_prefix(device, evse):
    assert await device.login()
    assert await until(lambda: device.get_latest_device_info().nickname == "Garage")
    await device.set_nickname("Shed #2 (b)")
    assert await until(lambda: evse.payloads[CommandEnum.NICKNAME_REQUEST][:1] == bytes([Action.SET]))
    assert evse.payloads[CommandEnum.NICKNAME_REQUEST] == (bytes([Action.SET]) + b"ACP#Shed #2 (b)").ljust(33, b"\0")


@pytest.mark.parametrize("nickname", ["Garáž", "x" * 12, "tab\there"])
async def test_a_nickname_the_charger_would_not_show_is_refused(device, evse, nickname):
    """An EC311S refuses names over 11 characters, and its display leaves non-ASCII characters blank."""
    assert await device.login()
    with pytest.raises(ValueError):
        await device.set_nickname(nickname)


async def test_a_nickname_reads_back_as_utf8(listener, listen_port):
    evse = await FakeEvse(SERIAL, nickname="Garáž", client_port=listen_port).start()
    try:
        device = await listener.async_add_device("127.0.0.1", "123456")
        assert await until(lambda: device.serial is not None)
        assert await device.login()
        assert await until(lambda: device.get_latest_device_info().nickname == "Garáž")
    finally:
        evse.stop()


async def test_language_is_read_at_login_and_reread_after_a_set(device, evse, fast_rereads):
    assert await device.login()
    assert await until(lambda: device.get_latest_device_info().language == LanguageEnum.ENGLISH)

    assert await device.set_language(LanguageEnum.GERMAN)
    assert await until(lambda: device.get_latest_device_info().language == LanguageEnum.GERMAN)
    assert evse.language == 3
    assert await until(lambda: evse.received.count(CommandEnum.LANGUAGE_REQUEST) == 3), "no get after the set"
    with pytest.raises(ValueError):
        await device.set_language(LanguageEnum.UNKNOWN)


@pytest.mark.parametrize("setter", ["language", "plan"])
async def test_a_set_reply_alone_updates_the_setting(device, evse, monkeypatch, setter):
    """An EC311S echoes the written value in its set reply; the re-read is only a second chance."""
    monkeypatch.setattr(device_module, "SETTING_REREAD_DELAY", 60)
    assert await device.login()
    assert await until(lambda: device.get_latest_weekly_plan() is not None)
    if setter == "language":
        await device.set_language(LanguageEnum.POLISH)
        assert await until(lambda: device.get_latest_device_info().language == LanguageEnum.POLISH)
    else:
        monday = WeeklyPlanSlot(enabled=True, start=time(7, 0), duration_minutes=30)
        await device.set_weekly_plan(WeeklyPlan(slots=[monday] + [WeeklyPlanSlot()] * 6))
        assert await until(lambda: device.get_latest_weekly_plan().slots[0] == monday)


async def test_weekly_plan_is_read_at_login_and_reread_after_a_set(device, evse, fast_rereads):
    assert await device.login()
    assert await until(lambda: device.get_latest_weekly_plan() is not None)
    assert not any(slot.enabled for slot in device.get_latest_weekly_plan().slots)

    friday = WeeklyPlanSlot(enabled=True, start=time(18, 0), duration_minutes=120)
    plan = WeeklyPlan(slots=[WeeklyPlanSlot()] * 4 + [friday] + [WeeklyPlanSlot()] * 2)
    assert await device.set_weekly_plan(plan)
    assert await until(lambda: device.get_latest_weekly_plan().slots[4].enabled)
    assert device.get_latest_weekly_plan() == plan
    assert await until(lambda: evse.received.count(CommandEnum.WEEKLY_PLAN_REQUEST) == 3), "no get after the set"


async def test_an_enabled_day_without_a_duration_is_refused(device, evse):
    assert await device.login()
    plan = WeeklyPlan(slots=[WeeklyPlanSlot(enabled=True, start=time(8, 0))] + [WeeklyPlanSlot()] * 6)
    with pytest.raises(ValueError, match="duration"):
        await device.set_weekly_plan(plan)


async def test_settings_survive_a_new_login_payload(device, evse):
    """A login payload carries no settings; merging it must not reset the ones read since."""
    assert await device.login()
    assert await until(lambda: device.get_latest_device_info().language == LanguageEnum.ENGLISH)
    evse.send(CommandEnum.LOGIN_EVENT, device_info_payload())
    assert await until(lambda: sum(name == "EvseDeviceInfo" for name, _ in device.events) >= 4)
    info = device.get_latest_device_info()
    assert (info.language, info.nickname) == (LanguageEnum.ENGLISH, "Garage")


async def test_a_closed_device_sends_no_pending_reread(device, evse):
    assert await device.login()
    await device.set_language(LanguageEnum.GERMAN)
    await device.set_weekly_plan(WeeklyPlan(slots=[WeeklyPlanSlot()] * 7))
    device.close()
    assert len(device._rereads) == 2
    assert all(timer.cancelled() for timer in device._rereads.values()), "a closed device would still send"
