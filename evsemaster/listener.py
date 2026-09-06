"""One UDP socket for every EVSE on the network, routed by device serial."""

import asyncio
import errno
import logging
import socket

from .data_types import CommandEnum, DataPacket, DiscoveredDevice
from .device import EvseDevice
from .protocol import ZERO_SERIAL, build_packet, parse_device_info

log = logging.getLogger(__name__)

LISTEN_PORT = 28376
PROBE_PORT = 7248


class DeviceAlreadyRegistered(ValueError):
    """Raised when a host is added twice; carries the device that already holds it."""

    def __init__(self, device: EvseDevice):
        super().__init__(f"EVSE {device.host_ip} is already registered with this listener")
        self.device = device


class _EVSEDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, parent: EvseListener):
        self.parent = parent
        self.closed = asyncio.Event()

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        self.parent._on_datagram(data, addr)

    def error_received(self, exc):  # type: ignore[override]
        # ICMP port unreachable arrives here when a device is off; not worth an error.
        log.debug(f"Datagram error received: {exc}")

    def connection_lost(self, exc):  # type: ignore[override]
        log.info("Datagram connection lost")
        self.closed.set()
        self.parent._transport = None


class EvseListener:
    """Owns the listen socket and routes packets to the right EvseDevice."""

    def __init__(
        self,
        listen_port: int = LISTEN_PORT,
        bind_addr: str = "0.0.0.0",
        on_discovery: callable | None = None,
    ):
        self.listen_port = listen_port
        self.bind_addr = bind_addr
        self._on_discovery = on_discovery
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _EVSEDatagramProtocol | None = None
        self._by_serial: dict[str, EvseDevice] = {}
        self._by_ip: dict[str, EvseDevice] = {}
        self._discovered: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def devices(self) -> list[EvseDevice]:
        """Every registered device, regardless of how it is indexed."""
        return list({id(device): device for device in (*self._by_ip.values(), *self._by_serial.values())}.values())

    @property
    def is_running(self) -> bool:
        return self._transport is not None

    async def start(self) -> bool:
        """Bind the listen socket."""
        if self._transport:
            return True
        loop = asyncio.get_running_loop()
        try:
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: _EVSEDatagramProtocol(self),
                local_addr=(self.bind_addr, self.listen_port),
                # a config entry reload can rebind before the loop released the old socket
                reuse_port=True if hasattr(socket, "SO_REUSEPORT") else None,
                allow_broadcast=True,
            )
            log.info("Listening on %s:%d", self.bind_addr, self.listen_port)
            return True
        except OSError as err:
            if err.errno == errno.EADDRINUSE:
                log.error(
                    f"UDP port {self.listen_port} is already in use by another application, "
                    "cannot listen for EVSE packets"
                )
            else:
                log.error(f"Failed to create datagram endpoint on port {self.listen_port}: {err}")
            return False

    async def stop(self) -> None:
        """Close the socket and drop all devices."""
        transport, self._transport = self._transport, None
        protocol, self._protocol = self._protocol, None
        if transport:
            transport.close()
        for device in self.devices:
            device.close()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._by_serial.clear()
        self._by_ip.clear()
        self._discovered.clear()
        if protocol:
            # close() releases the socket a loop iteration later; with SO_REUSEPORT an
            # immediate rebind would otherwise share the port with the dying socket and
            # the kernel would split incoming packets between the two.
            try:
                await asyncio.wait_for(protocol.closed.wait(), timeout=2)
            except TimeoutError:
                log.debug("Timed out waiting for the socket to close")

    async def async_add_device(self, host: str, password: str, on_event: callable | None = None) -> EvseDevice:
        """Register a device by host; the serial is learned from its first packet."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        host_ip = infos[0][4][0]
        if host_ip in self._by_ip:
            raise DeviceAlreadyRegistered(self._by_ip[host_ip])
        device = EvseDevice(self, host=host, host_ip=host_ip, password=password, on_event=on_event)
        self._by_ip[host_ip] = device
        log.debug("Registered %s", device)
        return device

    async def async_remove_device(self, device: EvseDevice) -> None:
        """Unregister a device so its packets become discovery events again."""
        device.close()
        self._by_ip.pop(device.host_ip, None)
        if device.serial:
            self._by_serial.pop(device.serial, None)
            self._discovered.discard(device.serial)
        log.debug("Removed %s", device)

    def get_device(self, serial: str) -> EvseDevice | None:
        """Look up a registered device by serial."""
        return self._by_serial.get(serial)

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self._transport:
            log.error("Cannot send to %s: listener is not running", addr)
            return
        try:
            self._transport.sendto(data, addr)
        except Exception as e:
            log.error(f"Failed to send packet to {addr}: {e}")

    async def probe(self, broadcast_addr: str = "255.255.255.255", port: int = PROBE_PORT) -> None:
        """Provoke an answer from every EVSE on the network.

        Sent with a zero serial and no password, so the devices answer with a password error
        rather than us broadcasting a real password. Their serial is in the reply header.
        """
        log.debug("Probing %s:%d for EVSEs", broadcast_addr, port)
        self.sendto(build_packet(CommandEnum.LOGIN_REQUEST, None, None), (broadcast_addr, port))

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            packet = DataPacket(data)
        except ValueError as e:
            log.debug(f"Ignoring packet from {addr[0]}:{addr[1]}: {e}")
            return

        device = self._route(packet, addr)
        if device is None:
            self._discover(packet, addr)
            return

        old_serial, old_ip = device.serial, device.host_ip
        device._bind(packet.device_serial, addr)
        if device.serial != old_serial or device.host_ip != old_ip:
            self._reindex(device, old_serial, old_ip)

        task = asyncio.create_task(device._handle_packet(packet))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _route(self, packet: DataPacket, addr: tuple[str, int]) -> EvseDevice | None:
        """Serial first so a device that changed IP is still found."""
        device = self._by_serial.get(packet.device_serial)
        if device:
            return device
        device = self._by_ip.get(addr[0])
        if device and device.serial not in (None, packet.device_serial):
            # a different charger answers at this address now
            return None
        return device

    def _reindex(self, device: EvseDevice, old_serial: str | None, old_ip: str) -> None:
        if device.serial != old_serial:
            if old_serial:
                self._by_serial.pop(old_serial, None)
            if device.serial:
                self._by_serial[device.serial] = device
        if device.host_ip != old_ip:
            if self._by_ip.get(old_ip) is device:
                del self._by_ip[old_ip]
            self._by_ip[device.host_ip] = device

    def _discover(self, packet: DataPacket, addr: tuple[str, int]) -> None:
        """Report an EVSE we are not managing, once per serial."""
        serial = packet.device_serial
        if not self._on_discovery or serial in self._discovered:
            return
        if serial == ZERO_SERIAL:
            # our own broadcast probe, or a device that will not say who it is
            log.debug("Ignoring packet without a serial from %s:%d", addr[0], addr[1])
            return
        self._discovered.add(serial)
        discovered = DiscoveredDevice(serial_number=serial, host=addr[0], port=addr[1])
        if packet.command in (CommandEnum.LOGIN_EVENT, CommandEnum.LOGIN_SUCCESS_EVENT):
            info = parse_device_info(packet)
            if info:
                discovered.brand = info.brand
                discovered.model = info.model
                discovered.hardware_version = info.hardware_version
                discovered.max_amps = info.max_amps
        log.info("Discovered EVSE %s at %s:%d", serial, addr[0], addr[1])
        try:
            self._on_discovery(discovered)
        except Exception as e:
            log.error(f"Error in discovery callback: {e}")
