from dataclasses import dataclass, field
from struct import unpack
import logging
from enum import IntEnum
from datetime import datetime
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class BaseSchema(BaseModel):
    """Base schema for all data types."""

    class Config:
        str_strip_whitespace = True


class CommandEnum(IntEnum):
    # request = client-initiated (you send)
    # event = anything incoming (EVSE -> you), whether it’s a reply or unsolicited
    # response = what you send back to an event that requires it

    NOT_LOGGED_IN_EVENT = 0x0001
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
    CURRENT_CHARGING_STATUS_EVENT = 0x0005  #  Always incoming, sent automatically by EVSE
    CURRENT_CHARGING_STATUS_RESPONSE = 0x0006

    # Charge control commands
    CHARGE_START_REQUEST = 0x8007
    CHARGE_START_RESPONSE = 0x0007
    CHARGE_STOP_REQUEST = 0x8008
    CHARGE_STOP_RESPONSE = 0x0008

    #### Haven't touched yet ####

    # Charge record commands
    CURRENT_CHARGE_RECORD = 0x0009
    UPLOAD_LOCAL_CHARGE_RECORD = 0x000A
    REQUEST_STATUS_RECORD = 0x000D
    REQUEST_CHARGE_STATUS_RECORD = 0x800D
    CURRENT_CHARGE_RECORD_RESPONSE = 0x8009

    # System settings commands
    SET_AND_GET_SYSTEM_TIME = 33025
    SET_AND_GET_SYSTEM_TIME_RESPONSE = 257
    GET_VERSION = 33030
    GET_VERSION_RESPONSE = 262
    SET_AND_GET_OUTPUT_ELECTRICITY = 33031
    SET_AND_GET_OUTPUT_ELECTRICITY_RESPONSE = 263
    SET_AND_GET_NICK_NAME = 33032
    SET_AND_GET_NICK_NAME_RESPONSE = 264
    SET_AND_GET_OFF_LINE_CHARGE = 33037
    SET_AND_GET_OFF_LINE_CHARGE_RESPONSE = 269
    SET_AND_GET_LANGUAGE = 33039
    SET_AND_GET_LANGUAGE_RESPONSE = 271
    SET_AND_GET_TEMPERATURE_UNIT = 33042
    SET_AND_GET_TEMPERATURE_UNIT_RESPONSE = 274

    # Fee and strategy commands
    SET_AND_GET_CHARGE_FEE_RESPONSE = 0x0104
    SET_AND_GET_SERVICE_FEE_RESPONSE = 0x0105
    SET_AND_GET_ALARM_CHARGE_STRATEGY_RESPONSE = 0x010E

    # Error responses
    PASSWORD_ERROR_RESPONSE = 341


class PlugStateEnum(IntEnum):
    DISCONNECTED = 1
    CONNECTED_UNLOCKED = 2
    CONNECTED_LOCKED = 4


class CurrentStateEnum(IntEnum):
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


class EvseDeviceInfo(BaseSchema):
    type: int
    brand: str
    model: str
    hardware_version: str
    max_power: int
    max_amps: int


class EvseStatus(BaseSchema):
    line_id: int  # no idea what this is, but its in the data
    inner_temperature: float
    outer_temperature: float
    emergency_stop: bool
    plug_state: PlugStateEnum
    output_state: int
    current_state: CurrentStateEnum
    errors: int
    l1_voltage: float = Field(default=0.0)
    l1_amps: float = Field(default=0.0)
    l2_voltage: float = Field(default=0.0)
    l2_amps: float = Field(default=0.0)
    l3_voltage: float = Field(default=0.0)
    l3_amps: float = Field(default=0.0)
    current_power: int = Field(default=0)
    total_kwh: float = Field(default=0)
    timestamp: datetime = Field(default_factory=datetime.now)


class ChargingStatus(BaseSchema):
    line_id: int
    current_state: CurrentStateEnum
    charge_id: str
    start_type: int
    charge_type: int
    max_duration_minutes: int | None = None
    max_energy_kwh: float | None = None
    charge_param3: float | None = None
    reservation_date: datetime
    user_id: str
    max_electricity: int
    start_date: datetime
    duration_seconds: int
    start_kwh_counter: float
    current_kwh_counter: float
    charge_kwh: float
    charge_price: float
    fee_type: int
    charge_fee: float
    timestamp: datetime = Field(default_factory=datetime.now)


class DataPacket:
    """Class for incomind data with unpack functions."""

    def __init__(self, data: bytes):
        if data is None or not isinstance(data, bytes):
            raise ValueError("Data must be a non-empty bytes object")
        if len(data) < 25:
            raise ValueError("Data must be at least 25 bytes long")
        # Check header
        header = unpack(">H", data[0:2])[0]
        if header != 0x0601:
            raise ValueError(f"Invalid header: {header:#04x}, expected 0x0601")
        self.command: CommandEnum = CommandEnum(unpack(">H", data[19:21])[0])
        if self.command not in CommandEnum:
            raise ValueError(f"Unknown command: {self.command}")
        # how its done in TS:
        # this.deviceSerial = buffer.toString("hex", 5, 13);
        self.device_serial = data[5:13].hex()  # Device serial number
        self.data = data[21:]  # drop all bytes before the data section
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
        return string_data.decode("ascii", errors="ignore")

    def get_buffer(self, offset: int, length: int = 1) -> bytes:
        """Read buffer from byte data."""
        end = offset + length
        return self.data[offset:end]

    def get_int(self, offset: int, length: int = 1) -> int:
        """Read integer from byte data."""
        end = offset + length
        return int.from_bytes(self.data[offset:end], byteorder="big", signed=False)

    def read_temperature(self, offset: int) -> float:
        """Read temperature from byte data."""
        temp = self.get_int(offset, 2)
        if temp == 0xFFFF:
            return -1.0
        return round((temp - 20000) / 100, 1)
