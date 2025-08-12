"""Simple EVSE protocol implementation for Home Assistant integration."""

import logging
import struct
import socket
from typing import Dict, Any, Optional
from datetime import datetime
import asyncio
import zoneinfo
from evsemaster.data_types import CommandEnum, DataPacket, EvseDeviceInfo, EvseStatus, ChargingStatus, CurrentStateEnum

log = logging.getLogger(__name__)


class SimpleEVSEProtocol:
    """Simple implementation of EVSE protocol for HA integration."""

    def __init__(self, host: str, password: str, event_callback: callable):
        """Initialize protocol handler."""
        self.host = host
        self.password = password
        self.event_callback = event_callback  # Callback for handling events
        self.listen_port = 28376  # Port to listen for incoming datagrams
        self.send_port = 7248  # Default port to send to (will be updated by discovery)
        self.serial_number = "00000000"  # Placeholder for device serial number
        self.user_id = "evsemaster_python"  # Default user ID
        self._listen_socket: Optional[socket.socket] = None
        self._send_socket: Optional[socket.socket] = None
        self._logged_in = False
        self._status: Optional[EvseStatus] = None
        self._device_info: Optional[EvseDeviceInfo] = None
        self._charging_status: Optional[ChargingStatus] = None
        self._discovery_running = False

    async def send_packet(self, data: bytes):
        """Send a packet to the EVSE."""
        if not self._send_socket:
            log.error("Send socket is not initialized")
            return

        try:
            self._send_socket.sendto(data, (self.host, self.send_port))
        except Exception as e:
            log.error("Failed to send packet: %s", e)

    async def connect(self) -> bool:
        """Connect to EVSE and start discovery."""
        try:
            # Create listen socket for receiving datagrams from EVSE
            self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listen_socket.bind(("0.0.0.0", self.listen_port))
            self._listen_socket.settimeout(10.0)

            # Create send socket for sending commands to EVSE
            self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._send_socket.settimeout(10.0)

            log.info(
                "Connected to EVSE at %s, listening on port %d",
                self.host,
                self.listen_port,
            )
            return True
        except Exception as err:
            log.error("Failed to connect: %s", err)
            return False

    async def disconnect(self):
        """Disconnect from EVSE."""
        self._discovery_running = False
        if self._listen_socket:
            self._listen_socket.close()
            self._listen_socket = None
        if self._send_socket:
            self._send_socket.close()
            self._send_socket = None
        self._logged_in = False

    async def login(self) -> bool:
        """Login to EVSE."""
        if not self._send_socket or not self._listen_socket:
            if not await self.connect():
                return False

        try:
            # Start discovery to find the correct port
            await self._discover_evse_port()

            await self.send_packet(self._build_packet(CommandEnum.LOGIN_REQUEST))

            # Try to receive response
            try:
                data, addr = self._listen_socket.recvfrom(1024)
                packet = DataPacket(data)  # Parse incoming data packet
                if self._parse_login_response(packet):
                    # If login response is successful, send confirm
                    await self.send_packet(self._build_packet(CommandEnum.LOGIN_CONFIRM_RESPONSE))
                    data, addr = self._listen_socket.recvfrom(1024)
                    data_packet = DataPacket(data)  # Parse confirm response
                    self._parse_login_response(data_packet)
                    self._logged_in = True
                    if data_packet.device_serial:
                        self.serial_number = data_packet.device_serial
                    log.info("Login successful")
                    # Start listener loop in background
                    asyncio.create_task(self.listener_loop())
                    return True
            except socket.timeout:
                log.warning("Login timeout")

            return False

        except Exception as err:
            log.exception("Failed to login: %s", err)
            return False

    async def listener_loop(self):
        """Run the listener loop to handle incoming datagrams."""
        if not self._listen_socket:
            return

        log.info("Starting listener loop on port %d", self.listen_port)
        while self._logged_in:
            try:
                data, addr = self._listen_socket.recvfrom(1024)
                try:
                    data_packet = DataPacket(data)  # Parse incoming data packet and slices the preamble
                except ValueError as e:
                    log.error(f"Invalid data packet received: {e}")
                    continue
                match data_packet.command:
                    case CommandEnum.HEADING_EVENT:
                        # response to keepalive, need to send back HEADING_RESPONSE
                        await self.send_packet(self._build_packet(CommandEnum.HEADING_RESPONSE))
                    case CommandEnum.NOT_LOGGED_IN_EVENT:
                        # not logged in, try to login
                        self._logged_in = False
                        log.warning("Received LOGIN command, not logged in, attempting to login")
                        await self.send_packet(self._build_packet(CommandEnum.LOGIN_REQUEST))
                    case CommandEnum.LOGIN_SUCCESS_EVENT:
                        # Handle login response
                        if self._parse_login_response(data_packet):
                            log.info("Login response received, sending confirm")
                            await self.send_packet(self._build_packet(CommandEnum.LOGIN_CONFIRM_RESPONSE))
                    case CommandEnum.CURRENT_STATUS_EVENT:
                        log.debug(self._parse_status_response(data_packet))
                        # Do we need to send a response?
                        await self.send_packet(self._build_packet(CommandEnum.CURRENT_STATUS_RESPONSE))
                        self.event_callback("status_update", self._status)
                    case CommandEnum.CURRENT_CHARGING_STATUS_EVENT:
                        log.debug(self._parse_ac_charging_status(data_packet))
                        self.event_callback("charging_status_update", self._charging_status)
                    case CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD:
                        pass
                    case _:
                        log.warning("Unhandled command: %s", data_packet.command.name)
                await asyncio.sleep(0.1)  # Avoid busy loop
            except socket.timeout:
                log.warning("Listener socket timeout, retrying...")
                continue
                await asyncio.sleep(1)  # Give the system some time to recover
            except Exception as e:
                log.error(f"Listener loop error: {e}, restarting.")
                await asyncio.sleep(1)  # Give the system some time to recover
                continue

    async def _discover_evse_port(self) -> bool:
        """Discover EVSE port by listening for incoming datagrams."""
        if not self._listen_socket:
            return False

        log.info("Starting EVSE port discovery on %s", self.host)

        # Send a discovery packet to the default port to trigger a response
        discovery_packet = self._build_packet(CommandEnum.LOGIN_REQUEST)
        if self._send_socket:
            self._send_socket.sendto(discovery_packet, (self.host, self.send_port))

        # Listen for any incoming datagram from our target EVSE
        for _ in range(5):  # Try for up to 5 attempts
            try:
                data, addr = self._listen_socket.recvfrom(1024)
                if addr[0] == self.host:
                    # Found a datagram from our EVSE, update the port
                    discovered_port = addr[1]
                    if discovered_port != self.send_port:
                        log.info(
                            "Discovered EVSE port: %d (was using %d)",
                            discovered_port,
                            self.send_port,
                        )
                        self.send_port = discovered_port
                    return True
            except socket.timeout:
                # Try sending another discovery packet
                if self._send_socket:
                    self._send_socket.sendto(discovery_packet, (self.host, self.send_port))
                continue

        log.warning("Could not discover EVSE port, using default %d", self.send_port)
        return False

    async def request_status(self) -> Dict[str, Any]:
        """Get EVSE status."""
        if not self._logged_in or not self._send_socket:
            return {}

        try:
            # Send status request
            await self.send_packet(self._build_packet(CommandEnum.CURRENT_STATUS_EVENT))

        except Exception as err:
            log.error("Failed to get status: %s", err)
            return {}

    async def start_charging(
        self, max_amps: int = 16, start_date: datetime = datetime.now(), duration_minutes: int = 65535
    ) -> bool:
        """Start charging."""
        if not self._logged_in or not self._send_socket:
            return False

        try:
            if self._status and self._status.current_state == CurrentStateEnum.CHARGING_RESERVATION:
                log.warning("Start charge send while a reservation is active, cancelling reservation first")
                await self.stop_charging()
            extra_payload = bytearray(47)

            # Line ID (seems to be always one 1, are there any devices with multiple lines?)
            struct.pack_into(">B", extra_payload, 0, 1)
            # User ID (16 bytes, ASCII encoded)
            struct.pack_into(">16s", extra_payload, 1, self.user_id.encode("ascii")[:16])
            # Charge ID (16 bytes, ASCII encoded)
            struct.pack_into(">16s", extra_payload, 17, start_date.strftime("%Y%m%d%H%M").encode("ascii")[:16])
            # Reservation: 0 for now, 1 if future reservation
            struct.pack_into(">B", extra_payload, 33, 0 if datetime.now() > start_date else 1)
            # Reservation date (current time in Shanghai epoch)
            struct.pack_into(">I", extra_payload, 34, self._datetime_to_shanghai_epoch(start_date))
            # Start type (always 1)
            struct.pack_into(">B", extra_payload, 38, 1)
            # Charge type (always 1)
            struct.pack_into(">B", extra_payload, 39, 1)
            # Max duration (65535 = highest possible, unlimited)
            struct.pack_into(">H", extra_payload, 40, duration_minutes)
            # Max energy (65535 = highest possible, unlimited)
            struct.pack_into(">H", extra_payload, 42, 65535)
            # Charge param 3 (always 65535)
            struct.pack_into(">H", extra_payload, 44, 65535)
            # Max electricity in amps
            struct.pack_into(">B", extra_payload, 46, max_amps)

            packet = self._build_packet(CommandEnum.CHARGE_START_REQUEST, extra_payload)
            await self.send_packet(packet)
            log.info("Sent charge start command")
            return True
        except Exception as err:
            log.error("Failed to start charging: %s", err)
            return False

    def _datetime_to_shanghai_epoch(self, dt: datetime) -> int:
        """
        Convert datetime to Shanghai timestamp.
        """
        log.debug(f"Converting time {dt.timestamp()} in {dt.tzinfo} to Shanghai epoch")
        shanghai_tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=shanghai_tz)
        elif dt.tzinfo.zone != "Asia/Shanghai":
            dt = dt.astimezone(shanghai_tz)
        log.debug(f"Converted time is now {dt.timestamp()} in {dt.tzinfo}")
        return int(dt.timestamp())

    async def stop_charging(self) -> bool:
        """Stop charging."""
        if not self._logged_in or not self._send_socket:
            return False

        try:
            extra_payload = bytearray(1)  # Extra payload for charge stop
            extra_payload[0] = 1  # Port to stop charging on
            packet = self._build_packet(CommandEnum.CHARGE_STOP_REQUEST, extra_payload)
            await self.send_packet(packet)
            log.info("Sent charge stop command")
            return True
        except Exception as err:
            log.error("Failed to stop charging: %s", err)
            return False

    def _build_packet(self, cmd: CommandEnum, payload: bytes = b"") -> bytes:
        """Generic method to build a packet with given command and payload."""
        packet = bytearray(25 + len(payload))

        # Header (0x0601)
        struct.pack_into(">H", packet, 0, 0x0601)
        # Length
        struct.pack_into(">H", packet, 2, len(packet))
        # Key type
        packet[4] = 0x00
        # Device serial (8 bytes) - use zeros for now
        # Password (6 bytes)
        if self.password:
            password_bytes = self.password.encode("ascii")[:6]
        else:
            password_bytes = b"\x00\x00\x00\x00\x00\x00"
        packet[13 : 13 + len(password_bytes)] = password_bytes
        # Command
        struct.pack_into(">H", packet, 19, cmd)
        # Payload
        packet[21 : 21 + len(payload)] = payload
        # Checksum
        checksum = sum(packet[:-4]) % 0xFFFF
        struct.pack_into(">H", packet, len(packet) - 4, checksum)
        # Tail (0x0f02)
        struct.pack_into(">H", packet, len(packet) - 2, 0x0F02)

        return bytes(packet)

    def _parse_login_response(self, data: DataPacket) -> bool:
        """Parse login response."""
        try:
            if data.command == CommandEnum.LOGIN_SUCCESS_EVENT:
                # Login response - extract device info
                self._parse_device_info(data)
                return True
            elif data.command == CommandEnum.PASSWORD_ERROR_EVENT:
                # Password error
                log.error("Password error received")
                return False

            return False

        except Exception as err:
            log.error("Failed to parse login response: %s", err)
            return False

    def _parse_device_info(self, data: DataPacket):
        """Parse device information from login response."""
        try:
            if data.length() < 25:
                return

            self._device_info = EvseDeviceInfo(
                type=data.get_int(0, 1),
                brand=data.get_string(1, 16),
                model=data.get_string(17, 16),
                hardware_version=data.get_string(33, 16),
                max_power=data.get_int(49, 4),
                max_amps=data.get_int(53, 1),
            )

        except Exception as err:
            log.error("Failed to parse device info: %s", err)

    def _parse_status_response(self, data: DataPacket):
        """Parse status response."""
        try:
            if data.length() < 33:
                return

            self._status = EvseStatus(
                line_id=data.get_int(0, 1),
                l1_voltage=data.get_int(1, 2) / 10,
                l1_amps=data.get_int(3, 2) / 100,
                total_power=data.get_int(5, 4),
                total_kwh=data.get_int(9, 4) / 100,
                inner_temperature=data.read_temperature(13),
                outer_temperature=data.read_temperature(15),
                emergency_stop=data.get_int(17, 1),
                gun_state=data.get_int(18, 1),
                output_state=data.get_int(19, 1),
                current_state=data.get_int(20, 1),
                errors=data.get_int(21, 4),
                l2_voltage=data.get_int(25, 2) / 10,
                l2_amps=data.get_int(27, 2) / 100,
                l3_voltage=data.get_int(29, 2) / 10,
                l3_amps=data.get_int(31, 2) / 100,
            )

            return self._status

        except Exception as err:
            log.error("Failed to parse status response: %s", err)

    def _parse_ac_charging_status(self, data: DataPacket):
        """Parse AC charging status response."""
        try:
            if data.length() < 25:
                return

            self._charging_status = ChargingStatus(
                line_id=data.get_int(0, 1),
                current_state=data.get_int(1, 1),
                charge_id=data.get_string(2, 16),
                start_type=data.get_int(18, 1),
                charge_type=data.get_int(19, 1),
                max_duration_minutes=None if data.get_int(20, 2) == 65535 else data.get_int(20, 2),
                max_energy_kwh=None if data.get_int(22, 2) == 65535 else data.get_int(22, 2) * 0.01,
                charge_param3=None if data.get_int(24, 2) == 65535 else data.get_int(24, 2) * 0.01,
                reservation_date=datetime.fromtimestamp(data.get_int(26, 4)),
                user_id=data.get_string(30, 16),
                max_electricity=data.get_int(46, 1),
                start_date=datetime.fromtimestamp(data.get_int(47, 4)),
                duration_seconds=data.get_int(51, 4),
                start_kwh_counter=data.get_int(55, 4) * 0.01,
                current_kwh_counter=data.get_int(59, 4) * 0.01,
                charge_kwh=data.get_int(63, 4) * 0.01,
                charge_price=data.get_int(67, 4) * 0.01,
                fee_type=data.get_int(71, 1),
                charge_fee=data.get_int(72, 2) * 0.01,
            )

            return self._charging_status

        except Exception as err:
            log.error("Failed to parse AC charging status: %s", err)

    def get_latest_device_info(self) -> Optional[EvseDeviceInfo]:
        """Get the latest device info."""
        return self._device_info

    def get_latest_status(self) -> Optional[EvseStatus]:
        """Get the latest EVSE status."""
        return self._status

    def get_latest_charging_status(self) -> Optional[ChargingStatus]:
        """Get the latest charging status."""
        return self._charging_status

    @property
    def is_logged_in(self) -> bool:
        """Check if logged in."""
        return self._logged_in
