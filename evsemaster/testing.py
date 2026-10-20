"""A fake EVSE for testing, for this library and for anything that builds on it.

The fake speaks enough of the protocol to drive a real client: it announces itself, answers
logins, keeps a session alive with headings, and records what it received.

    listener = EvseListener()
    await listener.start()
    evse = await FakeEvse("aa" * 8).start()
    device = await listener.async_add_device("127.0.0.1", "123456")
    assert await device.login()
"""

import asyncio
import struct
import time

from .data_types import Action, CommandEnum
from .listener import LISTEN_PORT
from .protocol import build_packet, shanghai_offset


def device_info_payload(brand: str = "BESEN", model: str = "BS20", max_amps: int = 32) -> bytes:
    """Payload of a LOGIN_EVENT / LOGIN_SUCCESS_EVENT."""
    payload = bytearray(70)
    payload[1 : 1 + len(brand)] = brand.encode()
    payload[17 : 17 + len(model)] = model.encode()
    payload[33:39] = b"1.2.34"
    struct.pack_into(">I", payload, 49, 7400)
    struct.pack_into(">B", payload, 53, max_amps)
    return bytes(payload)


def status_payload(
    three_phase: bool = True,
    state: int = 13,
    plug: int = 4,
    power: int = 0,
    emergency_stop: bool = False,
    errors: int = 0,
) -> bytes:
    """Payload of a status event; single-phase chargers omit the L2/L3 block."""
    payload = bytearray(33 if three_phase else 25)
    struct.pack_into(">B", payload, 0, 1)
    struct.pack_into(">H", payload, 1, 2301)
    struct.pack_into(">H", payload, 3, 1600)
    struct.pack_into(">I", payload, 5, power)
    struct.pack_into(">I", payload, 9, 123456)
    struct.pack_into(">H", payload, 13, 22500)
    struct.pack_into(">H", payload, 15, 22000)
    struct.pack_into(">B", payload, 17, 0 if emergency_stop else 1)
    struct.pack_into(">B", payload, 18, plug)
    struct.pack_into(">B", payload, 19, 1)
    struct.pack_into(">B", payload, 20, state)
    struct.pack_into(">I", payload, 21, errors)
    if three_phase:
        struct.pack_into(">H", payload, 25, 2302)
        struct.pack_into(">H", payload, 27, 1500)
        struct.pack_into(">H", payload, 29, 2303)
        struct.pack_into(">H", payload, 31, 1400)
    return bytes(payload)


def charging_payload(amps: int = 16, duration: int = 3600, kwh: int = 1234) -> bytes:
    """Payload of a charging status event."""
    payload = bytearray(74)
    struct.pack_into(">B", payload, 0, 1)
    struct.pack_into(">B", payload, 1, 14)
    payload[2:12] = b"2026090412"
    struct.pack_into(">H", payload, 20, 65535)
    struct.pack_into(">H", payload, 22, 65535)
    struct.pack_into(">H", payload, 24, 65535)
    struct.pack_into(">B", payload, 46, amps)
    struct.pack_into(">I", payload, 51, duration)
    struct.pack_into(">I", payload, 63, kwh)
    return bytes(payload)


def charge_record_payload(
    charge_id: str = "202610161200",
    start: int = 1_792_000_000,
    duration: int = 3600,
    kwh: int = 1234,
    stop_reason: int = 11,
    started_by: str = "evsemasterpy",
    stopped_by: str = "evsemasterpy",
) -> bytes:
    """A charge record as 0x0009 and 0x000A carry it, and 0x000C from byte 9; times are Shanghai epochs."""
    payload = bytearray(96)
    payload[0] = 1
    payload[1 : 1 + len(started_by)] = started_by.encode()
    payload[17 : 17 + len(stopped_by)] = stopped_by.encode()
    payload[33 : 33 + len(charge_id)] = charge_id.encode()
    struct.pack_into(">BBB", payload, 49, 0, 1, 1)
    struct.pack_into(">HHH", payload, 52, 65535, 65535, 65535)
    struct.pack_into(">BB", payload, 58, stop_reason, 1)
    struct.pack_into(">IIIII", payload, 60, 0, start, start + duration, duration, 100_000)
    struct.pack_into(">II", payload, 80, 100_000 + kwh, kwh)
    return bytes(payload)


class FakeEvse(asyncio.DatagramProtocol):
    """An EVSE on loopback. Bind each one to its own 127.0.0.x to run several at once."""

    def __init__(
        self,
        serial: str,
        password: str = "123456",
        ip: str = "127.0.0.1",
        client_port: int | None = None,
        three_phase: bool = True,
        nickname: str = "",
        max_amps: int = 32,
        brand: str = "BESEN",
        model: str = "BS20",
        state: int = 13,
        plug: int = 4,
        announce_interval: float = 0.5,
        heading_interval: float = 1.0,
        clock_skew: int = 0,
        errors: int = 0,
        language: int = 1,
        history: dict[int, bytes] | None = None,
        history_capacity: int = 300,
    ):
        self.serial = serial
        self.password = password
        self.ip = ip
        # resolved here, not in the signature, so a test can retarget the module constant
        self.client_port = LISTEN_PORT if client_port is None else client_port
        self.three_phase = three_phase
        self.nickname = nickname
        self.max_amps = max_amps
        self.brand = brand
        self.model = model
        self.state = state
        self.plug = plug
        self.announce_interval = announce_interval
        self.heading_interval = heading_interval
        self.clock_skew = clock_skew
        self.errors = errors
        self.language = language
        # every slot off, as an EC311S reports a plan nobody set
        self.weekly_plan = bytes([1, 0, 0, 0, 0, 0, 0, 0, 0]) * 7
        self.history = history or {}
        self.history_capacity = history_capacity
        # history requests the fake leaves unanswered once, as a real charger loses some
        self.drop_history_once: set[int] = set()
        self.drop_history_always: set[int] = set()
        self.received: list[CommandEnum] = []
        self.payloads: dict[CommandEnum, bytes] = {}
        self.amps_set: list[int] = []
        self.transport: asyncio.DatagramTransport | None = None
        self.client: tuple[str, int] | None = None
        self._heading_task: asyncio.Task | None = None
        self._announce_task: asyncio.Task | None = None

    async def start(self) -> FakeEvse:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(self.ip, 0), allow_broadcast=True
        )
        self._announce_task = asyncio.create_task(self._announcements())
        return self

    def stop(self) -> None:
        for task in (self._heading_task, self._announce_task):
            if task:
                task.cancel()
        self._heading_task = None
        if self.transport:
            self.transport.close()

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    @property
    def has_session(self) -> bool:
        """True once a client completed a login with us."""
        return self._heading_task is not None

    def send(self, cmd: CommandEnum, payload: bytes = b"") -> None:
        target = self.client or ("127.0.0.1", self.client_port)
        self.transport.sendto(build_packet(cmd, self.serial, self.password, payload), target)

    def announce(self) -> None:
        """The unsolicited broadcast a charger sends while it has no session."""
        self.transport.sendto(
            build_packet(
                CommandEnum.LOGIN_EVENT,
                self.serial,
                "",
                device_info_payload(brand=self.brand, model=self.model, max_amps=self.max_amps),
            ),
            ("127.0.0.1", self.client_port),
        )

    def broadcast_heading(self) -> None:
        """A heading arriving with no registration of ours behind it.

        Firmware broadcasts headings, so another client's registration delivers them too; a
        client must not read one as proof that the registration is its own.
        """
        self.transport.sendto(
            build_packet(CommandEnum.HEADING_EVENT, self.serial, ""),
            ("127.0.0.1", self.client_port),
        )

    def upload_record(self, record: bytes, current: bool = False) -> None:
        """A finished session's record: 0x000A, replayed by a real charger until acknowledged, or 0x0009."""
        if current:
            self.send(CommandEnum.CURRENT_CHARGE_RECORD_EVENT, record)
        else:
            self.send(CommandEnum.UPLOAD_LOCAL_CHARGE_RECORD_EVENT, record)

    def end_session(self) -> None:
        """Forget the client, as a charger does when it is power cycled."""
        if self._heading_task:
            self._heading_task.cancel()
            self._heading_task = None

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.client = addr
        cmd = CommandEnum(struct.unpack(">H", data[19:21])[0])
        self.received.append(cmd)
        payload = data[21:-4]
        self.payloads[cmd] = payload

        if cmd == CommandEnum.LOGIN_REQUEST:
            if data[13:19].decode(errors="ignore") != self.password:
                self.send(CommandEnum.PASSWORD_ERROR_EVENT)
                return
            self.send(
                CommandEnum.LOGIN_SUCCESS_EVENT,
                device_info_payload(brand=self.brand, model=self.model, max_amps=self.max_amps),
            )
        elif cmd == CommandEnum.LOGIN_CONFIRM_RESPONSE:
            if not self._heading_task:
                self._heading_task = asyncio.create_task(self._headings())
        elif cmd == CommandEnum.STATUS_REQUEST:
            self.send(
                CommandEnum.STATUS_EVENT,
                status_payload(self.three_phase, state=self.state, plug=self.plug, errors=self.errors),
            )
        elif cmd == CommandEnum.CHARGING_STATUS_REQUEST:
            self.send(CommandEnum.CHARGING_STATUS_EVENT, charging_payload())
        elif cmd == CommandEnum.SYSTEM_TIME_REQUEST:
            # reports its own clock even right after a set, as a skewed charger did in January 2026
            clock = int(time.time()) - shanghai_offset() + self.clock_skew
            self.send(CommandEnum.SYSTEM_TIME_EVENT, struct.pack(">BI", payload[0] if payload else Action.GET, clock))
        elif cmd == CommandEnum.NICKNAME_REQUEST:
            self.send(CommandEnum.NICKNAME_EVENT, bytes([Action.GET]) + self.nickname.encode() + b"\x00")
        elif cmd == CommandEnum.OUTPUT_AMPERAGE_REQUEST:
            if payload and payload[0] == Action.SET and len(payload) > 1 and payload[1]:
                self.amps_set.append(payload[1])
            amps = self.amps_set[-1] if self.amps_set else 16
            self.send(CommandEnum.OUTPUT_AMPERAGE_EVENT, bytes([Action.GET, amps]))
        elif cmd == CommandEnum.LANGUAGE_REQUEST:
            if payload[:1] == bytes([Action.SET]):
                self.language = payload[1]
            self.send(CommandEnum.LANGUAGE_EVENT, bytes([payload[0], self.language]))
        elif cmd == CommandEnum.WEEKLY_PLAN_REQUEST:
            if payload[:1] == bytes([Action.SET]):
                self.weekly_plan = payload[1:64]
            self.send(CommandEnum.WEEKLY_PLAN_EVENT, payload[:1] + self.weekly_plan)
        elif cmd == CommandEnum.HISTORY_COUNT_REQUEST:
            self.send(CommandEnum.HISTORY_COUNT_EVENT, struct.pack(">BIII", 0, self.history_capacity, 0, 0))
        elif cmd == CommandEnum.HISTORY_RECORD_REQUEST:
            index = struct.unpack(">I", payload[1:5])[0]
            if index in self.drop_history_always:
                return
            if index in self.drop_history_once:
                self.drop_history_once.discard(index)
                return
            record = self.history.get(index)
            header = struct.pack(">IIB", self.history_capacity, index, 1 if record else 3)
            self.send(CommandEnum.HISTORY_RECORD_EVENT, header + (record or bytes(96)))

    async def _announcements(self) -> None:
        while True:
            if not self._heading_task:
                self.announce()
            await asyncio.sleep(self.announce_interval)

    async def _headings(self) -> None:
        while True:
            self.send(CommandEnum.HEADING_EVENT)
            await asyncio.sleep(self.heading_interval)
