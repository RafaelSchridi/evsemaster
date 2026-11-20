"""Charger clock conversion across zones, DST switches, and the hours a switch skips or repeats."""

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evsemaster.protocol import datetime_to_device_epoch, device_epoch_to_datetime

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
ZONES = [
    "Europe/Amsterdam",
    "Europe/London",  # winter offset 0
    "Europe/Helsinki",
    "Australia/Sydney",  # southern hemisphere: summer time in January
    "Australia/Adelaide",  # half-hour offset
    "Australia/Lord_Howe",  # DST moves the clock by 30 minutes
    "Australia/Perth",  # no DST, same offset as Shanghai
    "Australia/Brisbane",  # no DST
    "UTC",
]


def charger_epoch(wall: datetime) -> int:
    """What the charger stores for a naive wall time: that time read as UTC, minus 8 h (docs/protocol.md)."""
    return calendar.timegm(wall.timetuple()) - 8 * 3600


@pytest.mark.parametrize("zone", ZONES)
def test_every_quarter_hour_of_a_year_keeps_its_wall_time(zone):
    tz = ZoneInfo(zone)
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    while instant.year == 2026:
        local = instant.astimezone(tz)
        epoch = datetime_to_device_epoch(instant, tz)
        assert epoch == charger_epoch(local.replace(tzinfo=None)), local

        read = device_epoch_to_datetime(epoch, tz)
        first_pass = local.replace(fold=0)
        assert read.timestamp() == first_pass.timestamp(), local
        assert read.utcoffset() == first_pass.utcoffset(), local
        instant += timedelta(minutes=15)


@pytest.mark.parametrize(
    ("zone", "wall", "offset_hours"),
    [
        ("Europe/Amsterdam", (2026, 7, 1, 5, 0), 2),
        ("Europe/Amsterdam", (2026, 1, 15, 5, 0), 1),
        ("Europe/London", (2026, 1, 15, 5, 0), 0),
        ("Australia/Sydney", (2026, 1, 15, 5, 0), 11),
        ("Australia/Sydney", (2026, 7, 1, 5, 0), 10),
        ("Australia/Adelaide", (2026, 1, 15, 5, 0), 10.5),
        ("Australia/Perth", (2026, 1, 15, 5, 0), 8),
        ("Australia/Sydney", (2027, 1, 1, 0, 30), 11),
        ("Europe/Amsterdam", (2026, 12, 31, 23, 59), 1),
        ("Australia/Sydney", (2028, 2, 29, 12, 0), 11),
    ],
    ids=[
        "amsterdam-summer",
        "amsterdam-winter",
        "london-winter-is-utc",
        "sydney-summer-in-january",
        "sydney-winter-in-july",
        "adelaide-half-hour",
        "perth-same-offset-as-shanghai",
        "sydney-new-year-while-utc-is-still-in-the-old-one",
        "amsterdam-last-minute-of-the-year",
        "sydney-leap-day",
    ],
)
def test_a_wall_time_is_written_and_read_with_its_own_offset(zone, wall, offset_hours):
    """The offset is the one at that date, whatever the date is today."""
    tz = ZoneInfo(zone)
    epoch = charger_epoch(datetime(*wall))

    assert datetime_to_device_epoch(datetime(*wall, tzinfo=tz), tz) == epoch
    read = device_epoch_to_datetime(epoch, tz)
    assert read.replace(tzinfo=None) == datetime(*wall)
    assert read.utcoffset() == timedelta(hours=offset_hours)


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("wall", [(2026, 1, 15, 5, 0), (2026, 7, 1, 5, 0)], ids=["january", "july"])
def test_the_system_zone_gives_what_naming_it_gives(system_zone, zone, wall):
    """tz=None is what 3.0.0 callers get, so it must not take a shortcut through the offset of now."""
    system_zone(zone)
    tz = ZoneInfo(zone)
    dt = datetime(*wall, tzinfo=tz)
    assert datetime_to_device_epoch(dt) == datetime_to_device_epoch(dt, tz)

    epoch = charger_epoch(datetime(*wall))
    from_system, from_name = device_epoch_to_datetime(epoch), device_epoch_to_datetime(epoch, tz)
    assert from_system == from_name
    assert from_system.utcoffset() == from_name.utcoffset()


@pytest.mark.parametrize(
    ("zone", "skipped", "written"),
    [
        ("Europe/Amsterdam", (2026, 3, 29, 2, 30), (2026, 3, 29, 3, 30)),
        ("Australia/Sydney", (2026, 10, 4, 2, 30), (2026, 10, 4, 3, 30)),
        ("Australia/Lord_Howe", (2026, 10, 4, 2, 15), (2026, 10, 4, 2, 45)),
    ],
)
def test_a_time_the_spring_switch_skips_is_written_after_it(system_zone, zone, skipped, written):
    """Python reads a skipped time with the offset before the switch, which lands after it."""
    tz = ZoneInfo(zone)
    dt = datetime(*skipped, tzinfo=tz)
    assert dt.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != datetime(*skipped), "not a skipped time"
    expected = charger_epoch(datetime(*written))

    assert datetime_to_device_epoch(dt, tz) == expected
    assert datetime_to_device_epoch(dt.astimezone(UTC), tz) == expected
    system_zone(zone)
    assert datetime_to_device_epoch(dt) == expected


@pytest.mark.parametrize(
    ("zone", "repeated"),
    [
        ("Europe/Amsterdam", (2026, 10, 25, 2, 30)),
        ("Australia/Sydney", (2026, 4, 5, 2, 30)),
        ("Australia/Lord_Howe", (2026, 4, 5, 1, 45)),
    ],
)
def test_the_repeated_hour_is_one_wall_time_read_as_its_first_pass(system_zone, zone, repeated):
    """The charger keeps wall time only, so it cannot tell the two passes apart."""
    tz = ZoneInfo(zone)
    first = datetime(*repeated, tzinfo=tz)
    second = first.replace(fold=1)
    assert first.utcoffset() != second.utcoffset(), "not a repeated time"
    epoch = charger_epoch(datetime(*repeated))

    assert datetime_to_device_epoch(first, tz) == epoch
    assert datetime_to_device_epoch(second, tz) == epoch
    read = device_epoch_to_datetime(epoch, tz)
    assert read.timestamp() == first.timestamp()
    assert read.utcoffset() == first.utcoffset()
    system_zone(zone)
    assert device_epoch_to_datetime(epoch).utcoffset() == first.utcoffset()


def test_a_clock_not_yet_moved_past_the_spring_switch_reads_as_the_time_after_it(system_zone):
    """Until a client resyncs it, the charger shows 02:30 on a morning that has no 02:30."""
    system_zone("Europe/Amsterdam")
    epoch = charger_epoch(datetime(2026, 3, 29, 2, 30))
    expected = datetime(2026, 3, 29, 3, 30, tzinfo=AMSTERDAM)

    for read in device_epoch_to_datetime(epoch, AMSTERDAM), device_epoch_to_datetime(epoch):
        assert read == expected
        assert read.replace(tzinfo=None) == datetime(2026, 3, 29, 3, 30)


@pytest.mark.parametrize(
    ("zone", "start", "end", "hours"),
    [
        ("Europe/Amsterdam", (2026, 3, 29, 1, 30), (2026, 3, 29, 3, 30), 1),
        ("Europe/Amsterdam", (2026, 10, 25, 1, 30), (2026, 10, 25, 3, 30), 3),
        ("Australia/Sydney", (2026, 10, 4, 1, 30), (2026, 10, 4, 3, 30), 1),
        ("Australia/Sydney", (2026, 4, 5, 1, 30), (2026, 4, 5, 3, 30), 3),
    ],
    ids=["amsterdam-spring", "amsterdam-autumn", "sydney-spring", "sydney-autumn"],
)
def test_times_read_across_a_switch_subtract_to_the_time_that_passed(zone, start, end, hours):
    """A session from 01:30 to 03:30 on a switch day did not last 2 hours."""
    tz = ZoneInfo(zone)
    read_start = device_epoch_to_datetime(charger_epoch(datetime(*start)), tz)
    read_end = device_epoch_to_datetime(charger_epoch(datetime(*end)), tz)

    assert read_end - read_start == timedelta(hours=hours)
    assert read_start < read_end
