"""Per-device EVSE session. Owns no socket; sends through the shared listener."""

import asyncio
import logging
import math
import struct
from datetime import datetime, timedelta
from typing import Any

from .capabilities import Capabilities
from .capabilities import for_device as capabilities_for
from .data_types import (
    Action,
    ChargeRecord,
    ChargingStatus,
    CommandEnum,
    CurrentStateEnum,
    DataPacket,
    EvseDeviceInfo,
    EvseStatus,
    LanguageEnum,
    NotLoggedInError,
    UnsupportedOperationError,
    WeeklyPlan,
    now_aware,
)
from .protocol import (
    WEEKLY_PLAN_SIZE,
    ZERO_SERIAL,
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
    shanghai_offset,
)

log = logging.getLogger(__name__)

# A charger heads every ~10s, and only while it still counts us as its client. Three missed
# beats means it has dropped us; nothing else proves otherwise, because an answer to a
# request we sent shows only that it is reachable.
HEADING_TIMEOUT = timedelta(seconds=35)
# A charger with no client announces every 3s; without this, one failing login retries on each.
LOGIN_COOLDOWN = timedelta(seconds=15)
LOGIN_RETRY_INTERVAL = 3
LOGIN_ATTEMPTS = 4

DEFAULT_SEND_PORT = 7248

# Clients rewrite the clock often (the vendor app every 10 s), so a smaller skew is drift or a timezone disagreement
# that the next write moves again. Only a skew that survives our own write can be aimed at.
CLOCK_SKEW_THRESHOLD = 86400

HISTORY_REQUEST_INTERVAL = 0.25  # 4/s lost ~6% of replies on an EC311S; the misses are asked again
HISTORY_REPLY_WAIT = 2.0
HISTORY_PASSES = 4

SETTING_REREAD_DELAY = 2  # set replies echo the new value but can go missing, so a get follows every set
LANGUAGE_GET = bytes([Action.GET, 0])
WEEKLY_PLAN_GET = bytes([Action.GET]) + bytes(WEEKLY_PLAN_SIZE - 1)  # the app's padding; an EC311S takes a bare [2]


class EvseDevice:
    """A single EVSE: session state, commands and parsed device state."""

    def __init__(
        self,
        listener,
        host: str,
        host_ip: str,
        password: str,
        on_event: callable | None = None,
    ):
        self.host = host
        self.host_ip = host_ip
        self.password = password
        self.send_port = DEFAULT_SEND_PORT
        self.serial: str | None = None
        self.user_id = "evsemasterpy"  # Do all actions as this "user"
        self.on_event = on_event
        self._listener = listener
        self._status: EvseStatus | None = None
        self._device_info: EvseDeviceInfo | None = None
        self._charging_status: ChargingStatus | None = None
        self._weekly_plan: WeeklyPlan | None = None
        self._rereads: dict[CommandEnum, asyncio.TimerHandle] = {}
        self._history: dict[int, ChargeRecord | None] | None = None
        self._history_count: asyncio.Future | None = None
        self._history_lock = asyncio.Lock()
        self._last_alive: datetime | None = None
        self._last_heading: datetime | None = None
        self._last_login_attempt: datetime | None = None
        self._authenticated = False
        self._last_seen: datetime | None = None
        self._login_future: asyncio.Future | None = None
        self._login_lock = asyncio.Lock()
        self._auto_login_task: asyncio.Task | None = None
        self._time_delta = 0

    def __repr__(self) -> str:
        return f"EvseDevice({self.serial or self.host}@{self.host_ip}:{self.send_port})"

    @property
    def is_authorised(self) -> bool:
        """A login succeeded, so the charger will act on our commands.

        This does not expire: the password rides in every packet, so authorisation is per
        packet and a charger that stopped reporting still accepts a stop.
        """
        return self._authenticated

    @property
    def is_receiving(self) -> bool:
        """We logged in and the charger still holds a registration, so the data we hold is current.

        Judged on headings alone. A charger emits them only while it holds a registration,
        and it is the one packet we never request, so it can never be an answer to our own
        poll. Any-traffic liveness reads a dropped registration as healthy.
        """
        if not self._authenticated or self._last_heading is None:
            return False
        return now_aware() - self._last_heading < HEADING_TIMEOUT

    @property
    def is_logged_in(self) -> bool:
        """Deprecated alias for `is_receiving`; incorrect terminology for what it measures"""
        return self.is_receiving

    @property
    def is_charging(self) -> bool:
        """Whether the charger last reported an active charge."""
        return self._status is not None and self._status.current_state == CurrentStateEnum.CHARGING

    @property
    def last_seen(self) -> datetime | None:
        """When we last received any packet from this device."""
        return self._last_seen

    @property
    def last_alive(self) -> datetime | None:
        """When the charger last sent anything other than an announcement."""
        return self._last_alive

    @property
    def last_heading(self) -> datetime | None:
        """When the charger last headed us, which is what holds the registration open."""
        return self._last_heading

    @property
    def capabilities(self) -> Capabilities:
        """What this charger's model will accept; permissive until it has identified itself."""
        info = self._device_info
        return capabilities_for(info.brand if info else None, info.model if info else None)

    @property
    def time_delta(self) -> int:
        """Clock skew of the device in seconds; 0 when no workaround is active."""
        return self._time_delta

    def send_command(self, cmd: CommandEnum, payload: bytes = b"") -> None:
        """Build and send a command to this device."""
        self._listener.sendto(build_packet(cmd, self.serial, self.password, payload), (self.host_ip, self.send_port))

    def send_event(self, event_type: str, data: Any) -> None:
        """Hand a parsed model to the consumer."""
        if self.on_event:
            try:
                self.on_event(event_type, data)
            except Exception as e:
                log.error(f"Error in event callback: {e}")

    def _may_attempt_login(self) -> bool:
        """Throttle the 3s announcement beat down to one attempt per cooldown."""
        if self._login_lock.locked():
            return False
        last = self._last_login_attempt
        return last is None or now_aware() - last >= LOGIN_COOLDOWN

    async def login(self) -> bool:
        """Log in, retrying the request until the EVSE answers."""
        if self._login_lock.locked():
            log.debug("Login already in progress for %s", self)
            return self.is_receiving
        async with self._login_lock:
            self._last_login_attempt = now_aware()
            loop = asyncio.get_running_loop()
            self._login_future = loop.create_future()
            try:
                for attempt in range(1, LOGIN_ATTEMPTS + 1):
                    # The first request doubles as port/serial discovery: even a request sent to the
                    # wrong port provokes the broadcast that tells us where the device really listens.
                    self.send_command(CommandEnum.LOGIN_REQUEST)
                    try:
                        success = await asyncio.wait_for(
                            asyncio.shield(self._login_future), timeout=LOGIN_RETRY_INTERVAL
                        )
                    except TimeoutError:
                        log.debug("No login answer from %s (attempt %d/%d)", self, attempt, LOGIN_ATTEMPTS)
                        continue
                    if not success:
                        return False
                    await self.request_essentials()
                    return True
                log.warning("Login timeout for %s", self)
                return False
            except Exception as e:
                log.error(f"Login failed for {self}: {e}")
                return False
            finally:
                if not self._login_future.done():
                    self._login_future.cancel()
                self._login_future = None

    def close(self) -> None:
        """Cancel background work; the device stops sending after this."""
        self._authenticated = False
        if self._auto_login_task and not self._auto_login_task.done():
            self._auto_login_task.cancel()
        for timer in self._rereads.values():
            timer.cancel()

    def _bind(self, serial: str, addr: tuple[str, int]) -> None:
        """Learn serial and source address from an incoming packet."""
        self._last_seen = now_aware()
        if serial and serial != ZERO_SERIAL and serial != self.serial:
            log.debug("Bound %s to serial %s", self.host, serial)
            self.serial = serial
        if addr[0] != self.host_ip:
            log.info("EVSE %s moved from %s to %s", self.serial, self.host_ip, addr[0])
            self.host_ip = addr[0]
        if addr[1] != self.send_port:
            log.debug("Discovered/updated EVSE port %d (was %d)", addr[1], self.send_port)
            self.send_port = addr[1]

    async def _handle_packet(self, packet: DataPacket) -> None:
        """Dispatch one routed packet."""
        cmd = packet.command
        if cmd != CommandEnum.LOGIN_EVENT:
            self._last_alive = now_aware()
        try:
            if cmd == CommandEnum.HEADING_EVENT:
                self._last_heading = now_aware()
                self.send_command(CommandEnum.HEADING_RESPONSE)
            elif cmd == CommandEnum.LOGIN_SUCCESS_EVENT:
                self._update_device_info(parse_device_info(packet))
                self.send_command(CommandEnum.LOGIN_CONFIRM_RESPONSE)
                self._authenticated = True
                # the handshake is what registers us, so it opens the heading window itself
                self._last_heading = now_aware()
                if self._login_future and not self._login_future.done():
                    self._login_future.set_result(True)
            elif cmd == CommandEnum.LOGIN_EVENT:
                # A charger announces only while it has no client, so this contradicts a registration.
                self._update_device_info(parse_device_info(packet))
                if self.password and not self.is_receiving and self._may_attempt_login():
                    log.info("%s announced itself while we hold no registration, logging in", self)
                    self._auto_login_task = asyncio.create_task(self.login())
            elif cmd == CommandEnum.PASSWORD_ERROR_EVENT:
                log.error("Password error for %s", self)
                self._authenticated = False
                self._last_alive = None
                self._last_heading = None
                if self._login_future and not self._login_future.done():
                    self._login_future.set_result(False)
            elif cmd in (CommandEnum.CURRENT_STATUS_EVENT, CommandEnum.STATUS_EVENT):
                self._update_status(packet)
                if cmd == CommandEnum.CURRENT_STATUS_EVENT:
                    self.send_command(CommandEnum.CURRENT_STATUS_RESPONSE)
            elif cmd in (CommandEnum.CURRENT_CHARGING_STATUS_EVENT, CommandEnum.CHARGING_STATUS_EVENT):
                charging_status = parse_charging_status(packet, self._shanghai_epoch_to_datetime)
                if charging_status:
                    self._charging_status = charging_status
                    log.debug(f"Charging Status: \r\n {charging_status}")
                    self.send_event(ChargingStatus.__name__, charging_status)
                if cmd == CommandEnum.CURRENT_CHARGING_STATUS_EVENT:
                    # the app's payload; firmware over-reads short payloads, so not an empty one
                    self.send_command(CommandEnum.CURRENT_CHARGING_STATUS_RESPONSE, bytes([0]))
            elif cmd == CommandEnum.NICKNAME_EVENT:
                nickname = packet.get_string(1, packet.length() - 1)
                log.debug(f"Nickname: {nickname}")
                if self._device_info:
                    self._device_info.nickname = nickname
                    self.send_event(EvseDeviceInfo.__name__, self._device_info)
            elif cmd == CommandEnum.OUTPUT_AMPERAGE_EVENT:
                amperage = packet.get_int(1, 1)
                log.debug(f"Configured Max Amps: {amperage}A")
                if self._device_info:
                    self._device_info.configured_max_amps = amperage
                    self.send_event(EvseDeviceInfo.__name__, self._device_info)
            elif cmd == CommandEnum.LANGUAGE_EVENT:
                if packet.length() >= 2 and self._device_info:
                    self._device_info.language = LanguageEnum(packet.get_int(1, 1))
                    self.send_event(EvseDeviceInfo.__name__, self._device_info)
            elif cmd == CommandEnum.WEEKLY_PLAN_EVENT:
                plan = parse_weekly_plan(packet)
                if plan:
                    self._weekly_plan = plan
                    self.send_event(WeeklyPlan.__name__, plan)
            elif cmd == CommandEnum.SYSTEM_TIME_EVENT:
                self._handle_system_time(packet)
            elif cmd in (CommandEnum.CURRENT_CHARGE_RECORD_EVENT, CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT):
                self._handle_charge_record(packet, cmd)
            elif cmd == CommandEnum.HISTORY_COUNT_EVENT:
                if self._history_count and not self._history_count.done() and packet.length() >= 5:
                    self._history_count.set_result(packet.get_int(1, 4))
            elif cmd == CommandEnum.HISTORY_RECORD_EVENT:
                self._handle_history_record(packet)
            else:
                log.debug(f"Unhandled command: {cmd.name}")
        except Exception as err:
            log.error("Failed to handle %s from %s: %s", cmd.name, self, err)

    def _update_device_info(self, device_info: EvseDeviceInfo | None) -> None:
        """Merge freshly parsed device info, keeping fields the login payload does not carry."""
        if not device_info:
            return
        if self._device_info:
            device_info.nickname = self._device_info.nickname
            device_info.configured_max_amps = self._device_info.configured_max_amps
            device_info.language = self._device_info.language
        self._device_info = device_info
        self.send_event(EvseDeviceInfo.__name__, device_info)

    def _update_status(self, packet: DataPacket) -> None:
        status = parse_status(packet)
        if not status:
            return
        self._status = status
        log.debug(f"Current Status: \r\n {status}")
        self.send_event(EvseStatus.__name__, status)

    def _handle_system_time(self, packet: DataPacket) -> None:
        """Track device clock skew from a system time reply, set or get alike."""
        if packet.length() < 5:
            log.debug("System time event without a time (action: %d)", packet.get_int(0, 1))
            return
        device_epoch = packet.get_int(1, 4)
        if not device_epoch:
            return
        delta = device_epoch + shanghai_offset() - int(now_aware().timestamp())
        log.debug("Device clock is %+d s from ours (epoch %d)", delta, device_epoch)
        if abs(delta) <= CLOCK_SKEW_THRESHOLD:
            if self._time_delta:
                log.info("Device clock agrees with ours again, dropping the %d s correction", self._time_delta)
            self._time_delta = 0
            return
        if abs(delta - self._time_delta) > 60:
            # rounded up, so a schedule fires late rather than early
            self._time_delta = math.ceil(delta / 60) * 60
            log.info("Device clock is %d s off, correcting schedules for it", self._time_delta)

    def _handle_charge_record(self, packet: DataPacket, cmd: CommandEnum) -> None:
        """Hand a finished session to the consumer and acknowledge it, which ends its replay."""
        record = parse_charge_record(packet, 0, self._shanghai_epoch_to_datetime)
        if not record:
            return
        # the charger keeps it in its history, but the vendor app only ever lists records it acknowledged itself
        if cmd == CommandEnum.CURRENT_CHARGE_RECORD_EVENT:
            self.send_command(CommandEnum.CURRENT_CHARGE_RECORD_RESPONSE, build_record_ack(packet))
        else:
            self.send_command(CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_RESPONSE, build_record_ack(packet))
        self.send_event(ChargeRecord.__name__, record)

    def _handle_history_record(self, packet: DataPacket) -> None:
        if self._history is None or packet.length() < 9:
            return
        # flag 1 carries a record; 3 marks an empty slot or one past the capacity
        has_record = packet.get_int(8, 1) == 1
        record = parse_charge_record(packet, 9, self._shanghai_epoch_to_datetime) if has_record else None
        self._history[packet.get_int(4, 4)] = record

    async def request_status(self) -> bool:
        """Request current EVSE status and charging status."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before requesting status")
        self.send_command(CommandEnum.STATUS_REQUEST)
        self.send_command(CommandEnum.CHARGING_STATUS_REQUEST)
        return True

    async def request_essentials(self) -> bool:
        """Send some commands to get basic info and sync device time."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before getting essentials")
        # Sync device time first to ensure correct timestamps
        await self.set_device_time()
        self.send_command(CommandEnum.NICKNAME_REQUEST, bytes([Action.GET]))
        self.send_command(CommandEnum.OUTPUT_AMPERAGE_REQUEST, bytes([Action.GET]))
        self.send_command(CommandEnum.LANGUAGE_REQUEST, LANGUAGE_GET)
        self.send_command(CommandEnum.WEEKLY_PLAN_REQUEST, WEEKLY_PLAN_GET)
        return await self.request_status()

    async def start_charging(
        self,
        max_amps: int | None = None,
        start_date: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> bool:
        """Send start charging request.

        max_amps: limit current
        start_date: schedule start (now if None)
        duration_minutes: max duration (65535 = unlimited)
        """
        if not self.is_authorised:
            raise NotLoggedInError("Please login before starting charge")
        if not self._device_info:
            raise NotLoggedInError("No device info yet, cannot validate charge parameters")

        if self._status and self._status.current_state in (
            CurrentStateEnum.CHARGING_RESERVATION,
            CurrentStateEnum.COMPLETED,
            CurrentStateEnum.COMPLETED_FULL_CHARGE,
        ):
            log.warning("Start charge send while a reservation/complete flag is active, cancelling first")
            await self.stop_charging()

        if self._status and self._status.current_state == CurrentStateEnum.CHARGING and start_date:
            # already charging, cannot schedule so stop first
            log.warning("Start charge send while already charging, stopping first to schedule new start")
            await self.stop_charging()

        # handle defaults like this because you can force none otherwise
        if not start_date:
            start_date = now_aware()
        elif start_date.tzinfo is None:
            raise ValueError("start_date must be timezone aware datetime")
        if not duration_minutes or duration_minutes < 1 or duration_minutes > 65535:
            duration_minutes = 65535
        if not max_amps:
            max_amps = self._device_info.configured_max_amps or self._device_info.max_amps
        elif max_amps < 6 or max_amps > self._device_info.max_amps:
            raise ValueError(f"max_amps must be between 6 and {self._device_info.max_amps}")

        payload = build_start_charging_payload(
            user_id=self.user_id,
            start_date=start_date,
            start_epoch=self._datetime_to_shanghai_epoch(start_date, apply_epoch_adjustment=True),
            duration_minutes=duration_minutes,
            max_amps=max_amps,
        )

        log.info(f"Starting charge on {self}: {max_amps}A, start={start_date}, duration={duration_minutes}m")
        self.send_command(CommandEnum.CHARGE_START_REQUEST, payload)
        return True

    async def stop_charging(self) -> bool:
        """Send stop charging request."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before stopping charge")
        self.send_command(CommandEnum.CHARGE_STOP_REQUEST, build_stop_charging_payload(self.user_id))
        return True

    async def set_nickname(self, nickname: str) -> bool:
        """Set the EVSE nickname."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before setting nickname")

        max_display_nickname = 28
        if len(nickname) > max_display_nickname:
            log.warning(f"Nickname too long, truncating to {max_display_nickname} characters")
            nickname = nickname[:max_display_nickname]

        # Prepend "ACP#" prefix as required by the protocol
        full_nickname = "ACP#" + nickname

        extra_payload = bytearray(33)
        struct.pack_into(">B", extra_payload, 0, Action.SET)
        # Nickname (up to 20 bytes, ASCII encoded)
        struct.pack_into(f">{len(full_nickname)}s", extra_payload, 1, full_nickname.encode("ascii"))

        self.send_command(CommandEnum.NICKNAME_REQUEST, extra_payload)
        return True

    async def set_output_amperage(self, amperage: int) -> bool:
        """Set the EVSE output amperage limit."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before setting output amperage")
        if amperage < 6 or amperage > 32:
            raise ValueError("Amperage must be between 6 and 32")
        if self._device_info and amperage > self._device_info.max_amps:
            raise ValueError(f"Amperage exceeds device max of {self._device_info.max_amps}")
        if not self.capabilities.amps_while_charging and self.is_charging:
            raise UnsupportedOperationError(
                f"{self._device_info.brand} {self._device_info.model} applies the amperage only "
                "when a charge starts; stop and restart the charge to change it"
            )

        extra_payload = bytearray(3)
        struct.pack_into(">B", extra_payload, 0, Action.SET)
        struct.pack_into(">B", extra_payload, 1, amperage)

        self.send_command(CommandEnum.OUTPUT_AMPERAGE_REQUEST, extra_payload)
        return True

    async def set_language(self, language: LanguageEnum) -> bool:
        """Set the language of the charger's display."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before setting the language")
        if language == LanguageEnum.UNKNOWN:
            raise ValueError("UNKNOWN is not a language the charger can be set to")
        self.send_command(CommandEnum.LANGUAGE_REQUEST, bytes([Action.SET, language]))
        self._reread_later(CommandEnum.LANGUAGE_REQUEST, LANGUAGE_GET)
        return True

    async def set_weekly_plan(self, plan: WeeklyPlan) -> bool:
        """Replace the charger's weekly plan; all seven days are written at once."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before setting the weekly plan")
        for slot in plan.slots:
            if slot.enabled and not slot.duration_minutes:
                raise ValueError("An enabled day needs a duration of 1 to 1440 minutes")
        if self._time_delta:
            log.warning("%s runs its weekly plan on a clock %d s off from ours", self, self._time_delta)
        self.send_command(CommandEnum.WEEKLY_PLAN_REQUEST, build_weekly_plan_payload(plan))
        self._reread_later(CommandEnum.WEEKLY_PLAN_REQUEST, WEEKLY_PLAN_GET)
        return True

    def _reread_later(self, cmd: CommandEnum, payload: bytes) -> None:
        """Get a setting again shortly after setting it."""
        if timer := self._rereads.get(cmd):
            timer.cancel()
        self._rereads[cmd] = asyncio.get_running_loop().call_later(
            SETTING_REREAD_DELAY, self.send_command, cmd, payload
        )

    async def read_history(self) -> dict[int, ChargeRecord]:
        """Read every record in the charger's history ring, keyed by slot; takes about 90 s."""
        if not self.is_authorised:
            raise NotLoggedInError("Please login before reading history")
        async with self._history_lock:
            capacity = await self._read_history_capacity()
            self._history = {}
            try:
                missing = list(range(1, capacity + 1))
                for _ in range(HISTORY_PASSES):
                    for index in missing:
                        self.send_command(CommandEnum.HISTORY_RECORD_REQUEST, struct.pack(">BI", 1, index))
                        await asyncio.sleep(HISTORY_REQUEST_INTERVAL)
                    await asyncio.sleep(HISTORY_REPLY_WAIT)
                    missing = [index for index in missing if index not in self._history]
                    if not missing:
                        break
                if missing:
                    log.warning("No history reply for %d of %d slots from %s", len(missing), capacity, self)
                return {index: record for index, record in self._history.items() if record}
            finally:
                self._history = None

    async def _read_history_capacity(self) -> int:
        """The count reply's bytes 1-4, which hold the ring's capacity rather than a count."""
        for _ in range(HISTORY_PASSES):
            self._history_count = asyncio.get_running_loop().create_future()
            self.send_command(CommandEnum.HISTORY_COUNT_REQUEST, bytes([1]))
            try:
                return await asyncio.wait_for(self._history_count, HISTORY_REPLY_WAIT)
            except TimeoutError:
                continue
            finally:
                self._history_count = None
        raise TimeoutError(f"{self} did not answer the history count")

    async def set_device_time(self, dt: datetime | None = None) -> bool:
        """Set the EVSE device's internal clock.

        dt: datetime to set (defaults to current time)

        The device stores time in a weird way - we need to calculate the offset
        between local timezone and Shanghai timezone and apply it to the timestamp.
        """
        if not self.is_authorised:
            raise NotLoggedInError("Please login before setting device time")

        if not dt:
            dt = now_aware()

        shanghai_epoch = self._datetime_to_shanghai_epoch(dt)

        extra_payload = bytearray(5)
        # Action: set (1)
        struct.pack_into(">B", extra_payload, 0, Action.SET)
        # Timestamp as 4-byte unsigned int
        struct.pack_into(">I", extra_payload, 1, shanghai_epoch)

        self.send_command(CommandEnum.SYSTEM_TIME_REQUEST, extra_payload)
        log.info(f"Setting device time to {dt} (Shanghai epoch: {shanghai_epoch})")
        return True

    def _datetime_to_shanghai_epoch(self, dt: datetime, apply_epoch_adjustment: bool = False) -> int:
        """
        Convert local datetime to EVSE timestamp.
        The EVSE interprets all timestamps as if they were in Shanghai timezone.

        apply_epoch_adjustment: If True, applies a calculated time delta workaround of now + the time
        the device itself thinks it is (31 days offset as of Jan 2026)
        """
        epoch = int(dt.timestamp() - shanghai_offset())

        # FIRMWARE BUG WORKAROUND: add time delta if calculated and requested
        if self._time_delta != 0 and apply_epoch_adjustment:
            epoch += self._time_delta
            log.debug(f"Applied time delta workaround: adjusted epoch by +{self._time_delta} seconds")

        return epoch

    def _shanghai_epoch_to_datetime(self, evse_epoch_time: int) -> datetime:
        """
        Convert EVSE timestamp to local datetime.
        The EVSE stores timestamps as if they were in Shanghai timezone.
        """
        epoch = evse_epoch_time + shanghai_offset()

        # FIRMWARE BUG WORKAROUND: subtract time delta if calculated and out of sync by more than 1 day
        if self._time_delta != 0 and evse_epoch_time - int(now_aware().timestamp()) > 86400:
            epoch -= self._time_delta
            log.debug(f"Applied time delta workaround: adjusted epoch by -{self._time_delta} seconds")

        return datetime.fromtimestamp(epoch, tz=now_aware().tzinfo)

    def get_latest_device_info(self) -> EvseDeviceInfo | None:
        """Get the latest device info."""
        return self._device_info

    def get_latest_status(self) -> EvseStatus | None:
        """Get the latest EVSE status."""
        return self._status

    def get_latest_charging_status(self) -> ChargingStatus | None:
        """Get the latest charging status."""
        return self._charging_status

    def get_latest_weekly_plan(self) -> WeeklyPlan | None:
        """Get the latest weekly plan."""
        return self._weekly_plan
