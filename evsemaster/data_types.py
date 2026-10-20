import logging
from datetime import datetime, time
from enum import IntEnum
from struct import unpack

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

_unmapped: set[tuple[type, int]] = set()
# 21 byte header plus the 4 byte checksum and tail: the smallest a packet can be, payload or not
ENVELOPE_SIZE = 25
HEADER = 0x0601
TAIL = 0x0F02


def now_aware() -> datetime:
    """Get the current datetime with timezone info."""
    return datetime.now().astimezone()


class BaseSchema(BaseModel):
    """Base schema for all data types."""

    model_config = ConfigDict(str_strip_whitespace=True)


class Action(IntEnum):
    """First payload byte of a set/get command; the reply echoes it."""

    SET = 1
    GET = 2


class CommandEnum(IntEnum):
    # request = client-initiated (you send)
    # event = anything incoming (EVSE -> you), whether it’s a reply or unsolicited
    # response = what you send back to an event that requires it
    # login
    LOGIN_EVENT = 0x0001  # broadcast announcement; carries device info, same layout as LOGIN_SUCCESS_EVENT
    LOGIN_REQUEST = 0x8002
    LOGIN_SUCCESS_EVENT = 0x0002
    LOGIN_CONFIRM_RESPONSE = 0x8001
    PASSWORD_ERROR_EVENT = 0x0155

    # Heading commands
    HEADING_EVENT = 0x0003
    HEADING_RESPONSE = 0x8003

    # Status commands
    CURRENT_STATUS_EVENT = 0x0004
    CURRENT_STATUS_RESPONSE = 0x8004
    STATUS_REQUEST = 0x800D
    STATUS_EVENT = 0x000D  # reply to STATUS_REQUEST, same layout as 0x0004
    CURRENT_CHARGING_STATUS_EVENT = 0x0005  #  Always incoming, sent automatically by EVSE
    CURRENT_CHARGING_STATUS_RESPONSE = 0x8005
    CHARGING_STATUS_REQUEST = 0x8006
    CHARGING_STATUS_EVENT = 0x0006  # reply to CHARGING_STATUS_REQUEST, same layout as 0x0005

    # Charge control commands
    CHARGE_START_REQUEST = 0x8007
    CHARGE_START_EVENT = 0x0007
    CHARGE_STOP_REQUEST = 0x8008
    CHARGE_STOP_EVENT = 0x0008

    # Setters/Getters
    NICKNAME_REQUEST = 0x8108
    NICKNAME_EVENT = 0x0108
    OUTPUT_AMPERAGE_REQUEST = 0x8107
    OUTPUT_AMPERAGE_EVENT = 0x0107
    TEMPRATURE_UNIT_REQUEST = 0x8112
    TEMPRATURE_UNIT_EVENT = 0x0112
    VERSION_REQUEST = 0x8106
    VERSION_EVENT = 0x0106
    START_METHOD_REQUEST = 0x810D
    START_METHOD_EVENT = 0x010D
    WEEKLY_PLAN_REQUEST = 0x810E
    WEEKLY_PLAN_EVENT = 0x010E
    LANGUAGE_REQUEST = 0x810F
    LANGUAGE_EVENT = 0x010F
    RFID_GET_REQUEST = 0x8123
    RFID_GET_EVENT = 0x0123

    # Requests that rewrite the password, Wi-Fi, RFID cards, fees, firmware or undecoded settings stay out of this
    # enum on purpose, so nothing sends them by name. Their replies are listed so they never reach the packet dump.
    CHANGE_PASSWORD_EVENT = 0x0102
    CHARGE_FEE_EVENT = 0x0104
    SERVICE_FEE_EVENT = 0x0105
    WIFI_CONFIG_EVENT = 0x010A  # carries the Wi-Fi passphrase in plaintext
    RFID_SET_EVENT = 0x0122
    FIRMWARE_UPGRADE_START_EVENT = 0x0150
    FIRMWARE_UPGRADE_PACKAGE_EVENT = 0x0151
    FIRMWARE_UPGRADE_RESULT_EVENT = 0x0152
    FIRMWARE_UPGRADE_CANCEL_EVENT = 0x0153
    MANEUVER_EVENT = 0x0162

    # System time commands
    SYSTEM_TIME_REQUEST = 0x8101  # 33025
    SYSTEM_TIME_EVENT = 0x0101  # 257

    # Charge records
    CURRENT_CHARGE_RECORD_EVENT = 0x0009
    CURRENT_CHARGE_RECORD_RESPONSE = 0x8009
    UPLOAD_LOCAL_CHARGE_RECORD_EVENT = 0x000A
    UPLOAD_LOCAL_CHARGE_RECORD_RESPONSE = 0x800A
    HISTORY_COUNT_REQUEST = 0x800B
    HISTORY_COUNT_EVENT = 0x000B
    HISTORY_RECORD_REQUEST = 0x800C
    HISTORY_RECORD_EVENT = 0x000C


class FirmwareEnum(IntEnum):
    """Base without members, so subclasses can declare their own.

    Firmware reports values we have not mapped; one unknown byte must not cost the whole packet.
    """

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, int) and (cls, value) not in _unmapped:
            _unmapped.add((cls, value))
            log.warning("Unmapped %s value %s, please report it with your charger model", cls.__name__, value)
        return cls.UNKNOWN


class PlugStateEnum(FirmwareEnum):
    UNKNOWN = 999
    DISCONNECTED = 1
    CONNECTED_UNLOCKED = 2
    CONNECTED_LOCKED = 4


class CurrentStateEnum(FirmwareEnum):
    UNKNOWN = 999
    EVSE_FAULT = 1
    CHARGING_FAULT_2 = 2
    CHARGING_FAULT_3 = 3
    WAITING_FOR_SWIPE = 10
    WAITING_FOR_BUTTON = 11
    NOT_CONNECTED = 12
    READY_TO_CHARGE = 13
    CHARGING = 14
    COMPLETED = 15
    COMPLETED_FULL_CHARGE = 17
    CHARGING_RESERVATION = 20


class FaultEnum(IntEnum):
    """Fault n is bit 31 - n of the status error field; names are the vendor app's."""

    RELAY_STUCK_L1 = 0
    RELAY_STUCK_L2 = 1
    RELAY_STUCK_L3 = 2
    OFFLINE = 3
    CC_ERROR = 4
    CP_ERROR = 5
    EMERGENCY_STOP = 6
    OVER_TEMPERATURE_INNER = 7
    OVER_TEMPERATURE_OUTER = 8
    UNKNOWN_ERROR = 9
    LEAKAGE_PROTECTION = 10
    SHORT_CIRCUIT = 11
    OVER_CURRENT = 12
    NOT_GROUNDED = 13
    OVER_VOLTAGE = 14
    LOW_VOLTAGE = 15
    # the app names none of 16-23
    FAULT_16 = 16
    FAULT_17 = 17
    FAULT_18 = 18
    FAULT_19 = 19
    FAULT_20 = 20
    FAULT_21 = 21
    FAULT_22 = 22
    FAULT_23 = 23
    NO_VENTILATION = 24
    INPUT_POWER_ERROR = 25
    MAINS_OVERLOAD = 26
    DIODE_SHORT_CIRCUIT = 27
    RTC_FAILURE = 28
    FLASH_FAILURE = 29
    EEPROM_FAILURE = 30
    METERING_MODULE_FAILURE = 31


class LanguageEnum(FirmwareEnum):
    UNKNOWN = 999
    ENGLISH = 1
    ITALIAN = 2
    GERMAN = 3
    FRENCH = 4
    SPANISH = 5
    HEBREW = 6
    POLISH = 7
    TURKISH = 8
    CHINESE_TRADITIONAL = 9


class StartTypeEnum(FirmwareEnum):
    UNKNOWN = 999
    CLIENT = 1
    PLUG_AND_CHARGE = 3  # started by "PAP"
    SCHEDULE = 4  # started by "Clock": the weekly plan


class StopReasonEnum(FirmwareEnum):
    UNKNOWN = 999
    RESERVATION_CANCELLED = 0
    CLIENT = 11
    AUTO_STOP = 14
    ENERGY_LIMIT = 15
    TIME_LIMIT = 16
    UNPLUGGED = 17
    POWER_LOSS = 255


class EvseDeviceInfo(BaseSchema):
    type: int = Field(default=0)
    brand: str = Field(default="Brand")
    model: str = Field(default="Model")
    firmware_version: str = Field(default="0.0")  # what the vendor app shows as "Firmware version"
    max_power: int = Field(default=0)
    max_amps: int = Field(default=16)
    serial_number: str = Field(default="00000000")  # 8 byte hex string
    nickname: str = Field(default="")
    configured_max_amps: int = Field(default=16)  # User-configured max amps
    language: LanguageEnum | None = None


class DiscoveredDevice(BaseSchema):
    """An EVSE seen on the network that is not registered with the listener."""

    serial_number: str
    host: str
    port: int
    brand: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    max_amps: int | None = None


class EvseStatus(BaseSchema):
    line_id: int  # no idea what this is, but its in the data
    inner_temperature: float
    outer_temperature: float
    emergency_stop: bool
    plug_state: PlugStateEnum
    output_state: int
    current_state: CurrentStateEnum
    faults: list[FaultEnum] = Field(default_factory=list)  # every fault that is set, lowest number first
    l1_voltage: float = Field(default=0.0)
    l1_amps: float = Field(default=0.0)
    l2_voltage: float = Field(default=0.0)
    l2_amps: float = Field(default=0.0)
    l3_voltage: float = Field(default=0.0)
    l3_amps: float = Field(default=0.0)
    current_power: int = Field(default=0)
    total_kwh: float = Field(default=0)


class ChargingStatus(BaseSchema):
    line_id: int
    current_state: CurrentStateEnum
    charge_id: str
    start_type: int
    charge_type: int
    max_duration_minutes: int | None
    max_energy_kwh: float | None
    charge_param3: float | None
    reservation_datetime: datetime | None
    user_id: str
    max_electricity: int
    set_datetime: datetime | None
    duration_seconds: int
    start_kwh_counter: float
    current_kwh_counter: float
    charge_kwh: float
    charge_price: float
    fee_type: int
    charge_fee: float


class ChargeRecord(BaseSchema):
    """One finished session, as 0x0009, 0x000A and the history (0x000C) carry it."""

    line_id: int
    started_by: str
    stopped_by: str
    charge_id: str
    has_reservation: bool
    start_type: StartTypeEnum
    charge_type: int
    max_duration_minutes: int | None
    max_energy_kwh: float | None
    stop_reason: StopReasonEnum
    reservation_datetime: datetime | None
    start_datetime: datetime | None
    stop_datetime: datetime | None
    duration_seconds: int
    start_kwh_counter: float
    stop_kwh_counter: float
    charge_kwh: float


class ChargeHistory(BaseSchema):
    """A read of the history ring: records by slot, and the slots that never answered, whose sessions are missing."""

    records: dict[int, ChargeRecord]
    unread_slots: list[int]


class WeeklyPlanSlot(BaseSchema):
    enabled: bool = False
    start: time = time(0, 0)
    duration_minutes: int = Field(default=0, ge=0, le=1440)


class WeeklyPlan(BaseSchema):
    """Seven daily charge windows, Monday first; plugging in during one charges for the rest of it."""

    slots: list[WeeklyPlanSlot] = Field(min_length=7, max_length=7)


class DataPacket:
    """Class for incoming data with unpack functions."""

    def __init__(self, data: bytes):
        if data is None or not isinstance(data, bytes):
            raise ValueError("Data must be a non-empty bytes object")
        if len(data) < ENVELOPE_SIZE:
            raise ValueError(f"Data must be at least {ENVELOPE_SIZE} bytes long")
        # Check header
        header = unpack(">H", data[0:2])[0]
        if header != HEADER:
            raise ValueError(f"Invalid header: {header:#06x}, expected {HEADER:#06x}")
        raw_command = unpack(">H", data[19:21])[0]
        try:
            self.command: CommandEnum = CommandEnum(raw_command)
        except ValueError:
            # dumped so it can be decoded from an issue report; the password at 13:19 is masked out
            dump = data[:13].hex() + "xxxxxxxxxxxx" + data[19:].hex()
            raise ValueError(f"Unknown command: {raw_command:#06x}, {len(data)} bytes: {dump}") from None
        self.device_serial = data[5:13].hex()  # Device serial number
        declared = unpack(">H", data[2:4])[0]
        # a length shorter than the envelope, or longer than what arrived, cannot be the real end
        if declared >= ENVELOPE_SIZE and declared <= len(data):
            end = declared
        else:
            end = len(data)
        # Payload only: drop the 21 byte header and the trailing checksum + tail
        self.data = data[21 : end - 4]
        checksum, tail = unpack(">HH", data[end - 4 : end])
        # the vendor app drops these too
        if tail != TAIL or checksum != sum(data[: end - 4]) % 0xFFFF:
            raise ValueError(
                f"Bad tail or checksum on {self.command.name}: checksum {checksum:#06x}, tail {tail:#06x}, "
                f"declared {declared} bytes, received {len(data)}"
            )
        log.debug(self.__repr__())

    def __repr__(self):
        return f"DataPacket: {self.command.name}, s/n={self.device_serial}, len={len(self.data)}"

    def length(self) -> int:
        """Get the length of the data."""
        return len(self.data)

    def get_string(self, offset: int, length: int = 1) -> str:
        """Read string from byte data."""
        end = offset + length
        string_data = self.data[offset:end]
        null_pos = string_data.find(b"\x00")
        if null_pos >= 0:
            string_data = string_data[:null_pos]
        # the vendor app decodes every string field as UTF-8
        return string_data.decode("utf-8", errors="ignore")

    def get_buffer(self, offset: int, length: int = 1) -> bytes:
        """Read buffer from byte data."""
        end = offset + length
        return self.data[offset:end]

    def get_int(self, offset: int, length: int = 1) -> int:
        """Read integer from byte data."""
        end = offset + length
        return int.from_bytes(self.data[offset:end], byteorder="big", signed=False)

    def read_measurement(self, offset: int, length: int, divisor: int = 1) -> float:
        """Read a scaled measurement; an all-FF field means no reading and reads as 0, as the vendor app does."""
        raw = self.get_int(offset, length)
        if raw == (1 << 8 * length) - 1:
            return 0.0
        return raw / divisor

    def read_temperature(self, offset: int) -> float:
        """Read temperature from byte data."""
        temp = self.get_int(offset, 2)
        if temp == 0xFFFF:
            return -1.0
        return round((temp - 20000) / 100, 1)


class NotLoggedInError(Exception):
    """Exception raised when an operation is attempted without being logged in."""

    pass


class UnsupportedOperationError(Exception):
    """Raised when the charger's model is known to reject an operation in its current state."""

    pass
