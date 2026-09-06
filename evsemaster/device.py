"""Per-device EVSE session. Owns no socket; sends through the shared listener."""

import asyncio
import logging
import struct
from datetime import datetime, timedelta
from typing import Any

from .capabilities import Capabilities
from .capabilities import for_device as capabilities_for
from .data_types import (
    ChargingStatus,
    CommandEnum,
    CurrentStateEnum,
    DataPacket,
    EvseDeviceInfo,
    EvseStatus,
    NotLoggedInError,
    UnsupportedOperationError,
    now_aware,
)
from .protocol import (
    ZERO_SERIAL,
    build_packet,
    build_start_charging_payload,
    parse_charging_status,
    parse_device_info,
    parse_status,
    shanghai_offset,
)

log = logging.getLogger(__name__)

# The EVSE sends a heading every ~10s once it is talking to us, but skips some; no heading
# for this long means it has stopped answering.
SESSION_TIMEOUT = timedelta(seconds=30)
LOGIN_RETRY_INTERVAL = 3
LOGIN_ATTEMPTS = 4

DEFAULT_SEND_PORT = 7248


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
        self._last_heading: datetime | None = None
        self._authenticated = False
        self._last_seen: datetime | None = None
        self._login_future: asyncio.Future | None = None
        self._login_lock = asyncio.Lock()
        self._auto_login_task: asyncio.Task | None = None
        self._time_delta = 0

    def __repr__(self) -> str:
        return f"EvseDevice({self.serial or self.host}@{self.host_ip}:{self.send_port})"

    @property
    def is_logged_in(self) -> bool:
        """Both a successful login and a recent heading.

        The heading half proves the charger is alive but not that our password was accepted:
        headings are broadcasted to every host on the network.
        Only the login half proves the password, which is what the config flow checks.
        """
        if not self._authenticated or self._last_heading is None:
            return False
        return now_aware() - self._last_heading < SESSION_TIMEOUT

    @property
    def is_charging(self) -> bool:
        """Whether the charger last reported an active charge."""
        return self._status is not None and self._status.current_state == CurrentStateEnum.CHARGING

    @property
    def last_seen(self) -> datetime | None:
        """When we last received any packet from this device."""
        return self._last_seen

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

    async def login(self) -> bool:
        """Log in, retrying the request until the EVSE answers."""
        if self._login_lock.locked():
            log.debug("Login already in progress for %s", self)
            return self.is_logged_in
        async with self._login_lock:
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
        try:
            if cmd == CommandEnum.LOGIN_SUCCESS_EVENT:
                self._update_device_info(parse_device_info(packet))
                self.send_command(CommandEnum.LOGIN_CONFIRM_RESPONSE)
                self._authenticated = True
                self._last_heading = now_aware()
                if self._login_future and not self._login_future.done():
                    self._login_future.set_result(True)
            elif cmd == CommandEnum.LOGIN_EVENT:
                # Periodic broadcast announcement; arrives whether or not we are logged in.
                self._update_device_info(parse_device_info(packet))
                if self.password and not self.is_logged_in and not self._login_lock.locked():
                    log.info("%s announced itself while logged out, logging in", self)
                    self._auto_login_task = asyncio.create_task(self.login())
            elif cmd == CommandEnum.PASSWORD_ERROR_EVENT:
                log.error("Password error for %s", self)
                self._authenticated = False
                self._last_heading = None
                if self._login_future and not self._login_future.done():
                    self._login_future.set_result(False)
            elif cmd == CommandEnum.HEADING_EVENT:
                self._last_heading = now_aware()
                self.send_command(CommandEnum.HEADING_RESPONSE)
            elif cmd == CommandEnum.CURRENT_STATUS_EVENT:
                self._update_status(packet)
                self.send_command(CommandEnum.CURRENT_STATUS_RESPONSE)
            elif cmd == CommandEnum.REQUEST_STATUS_RECORD:
                # Some devices report status under this command; only trust an exact status payload.
                if packet.length() in (25, 33):
                    self._update_status(packet)
                else:
                    log.debug("Ignoring %s with %d byte payload", cmd.name, packet.length())
            elif cmd in (CommandEnum.CURRENT_CHARGING_STATUS_EVENT, CommandEnum.CURRENT_CHARGING_STATUS_EVENT_2):
                charging_status = parse_charging_status(packet, self._shanghai_epoch_to_datetime)
                if charging_status:
                    self._charging_status = charging_status
                    log.debug(f"Charging Status: \r\n {charging_status}")
                    self.send_event(ChargingStatus.__name__, charging_status)
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
            elif cmd == CommandEnum.SYSTEM_TIME_EVENT:
                self._handle_system_time(packet)
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
        """Track device clock skew from a system time reply."""
        action = packet.get_int(0, 1)
        if packet.length() < 5:
            log.debug(f"System time event received (action: {action})")
            return
        device_epoch = packet.get_int(1, 4)
        log.debug(
            f"Device time {'set' if action == CommandEnum.SET_ACTION else 'get'}: "
            f"{self._shanghai_epoch_to_datetime(device_epoch)} (epoch: {device_epoch})"
        )
        if action != CommandEnum.GET_ACTION:
            return
        delta = device_epoch + shanghai_offset() - int(now_aware().timestamp())
        # only update if > 1 day out and changed by > 1 minute
        if delta > 86400 and delta - self._time_delta > 60:
            delta += 60 - (delta % 60)  # round up to whole minutes
            self._time_delta = delta
            log.info(f"Calculated time delta: {self._time_delta} s")

    async def request_status(self) -> bool:
        """Request current EVSE status."""
        if not self.is_logged_in:
            raise NotLoggedInError("Please login before requesting status")
        self.send_command(CommandEnum.CURRENT_STATUS_EVENT)
        # get device time
        self.send_command(CommandEnum.SYSTEM_TIME_REQUEST, bytes([CommandEnum.GET_ACTION]))
        return True

    async def request_essentials(self) -> bool:
        """Send some commands to get basic info and sync device time."""
        if not self.is_logged_in:
            raise NotLoggedInError("Please login before getting essentials")
        # Sync device time first to ensure correct timestamps
        await self.set_device_time()
        self.send_command(CommandEnum.NICKNAME_REQUEST, bytes([CommandEnum.GET_ACTION]))
        self.send_command(CommandEnum.OUTPUT_AMPERAGE_REQUEST, bytes([CommandEnum.GET_ACTION]))
        self.send_command(CommandEnum.CURRENT_STATUS_EVENT)
        return True

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
        if not self.is_logged_in:
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
        if not self.is_logged_in:
            raise NotLoggedInError("Please login before stopping charge")
        self.send_command(CommandEnum.CHARGE_STOP_REQUEST, bytes([1]))  # port id
        return True

    async def set_nickname(self, nickname: str) -> bool:
        """Set the EVSE nickname."""
        if not self.is_logged_in:
            raise NotLoggedInError("Please login before setting nickname")

        max_display_nickname = 28
        if len(nickname) > max_display_nickname:
            log.warning(f"Nickname too long, truncating to {max_display_nickname} characters")
            nickname = nickname[:max_display_nickname]

        # Prepend "ACP#" prefix as required by the protocol
        full_nickname = "ACP#" + nickname

        extra_payload = bytearray(33)
        # Action: 0 = set, 1 = get
        struct.pack_into(">B", extra_payload, 0, CommandEnum.SET_ACTION)
        # Nickname (up to 20 bytes, ASCII encoded)
        struct.pack_into(f">{len(full_nickname)}s", extra_payload, 1, full_nickname.encode("ascii"))

        self.send_command(CommandEnum.NICKNAME_REQUEST, extra_payload)
        return True

    async def set_output_amperage(self, amperage: int) -> bool:
        """Set the EVSE output amperage limit."""
        if not self.is_logged_in:
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
        struct.pack_into(">B", extra_payload, 0, CommandEnum.SET_ACTION)
        struct.pack_into(">B", extra_payload, 1, amperage)

        self.send_command(CommandEnum.OUTPUT_AMPERAGE_REQUEST, extra_payload)
        return True

    async def set_device_time(self, dt: datetime | None = None) -> bool:
        """Set the EVSE device's internal clock.

        dt: datetime to set (defaults to current time)

        The device stores time in a weird way - we need to calculate the offset
        between local timezone and Shanghai timezone and apply it to the timestamp.
        """
        if not self.is_logged_in:
            raise NotLoggedInError("Please login before setting device time")

        if not dt:
            dt = now_aware()

        shanghai_epoch = self._datetime_to_shanghai_epoch(dt)

        extra_payload = bytearray(5)
        # Action: set (1)
        struct.pack_into(">B", extra_payload, 0, CommandEnum.SET_ACTION)
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
