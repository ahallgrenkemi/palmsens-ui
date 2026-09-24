# Communicates  with the arduino firmware #
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import threading
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None



_MODE_RE = re.compile(r"^\s*(?P<mode>HEATING|COOLING|ON TARGET)\b", re.IGNORECASE)
_SERIAL_READ_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class TemperatureSettings:
    port: str | None = None
    baud_rate: int = 9600
    tolerance_c: float = 0.5
    log_dir: str | None = None
    stop_on_abort: bool = True
    sync_channels: bool = False


@dataclass(frozen=True)
class TemperatureStatus:
    elapsed_s: float
    mode: str | None
    temperature_c: float
    setpoint_c: float
    active_setpoint_c: float | None
    feedforward: float | None
    integral: float | None
    pid: float | None
    pwm: int | None
    raw_line: str


@dataclass(frozen=True)
class TemperatureProgress:
    target_c: float
    temperature_c: float | None
    setpoint_c: float | None
    wait_elapsed_s: float
    message: str


@dataclass
class _SharedSerialConnection:
    serial: object
    users: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class TemperatureController:
    _connections: dict[tuple[str, int], _SharedSerialConnection] = {}
    _connections_lock = threading.Lock()

    def __init__(self, settings: TemperatureSettings):
        self.settings = settings
        self._connection = None
        self._connection_key = None
        self.started_at = None
        self.log_path = None
        self._log_handle = None

    def connect(self):
        if serial is None:
            raise RuntimeError("pyserial is required for temperature chamber control.")

        if self._connection is not None:
            return

        port = self.settings.port or self.find_arduino_port()
        if not port:
            raise RuntimeError("Could not find an Arduino serial port for the temperature chamber.")

        connection_key = (port.casefold(), self.settings.baud_rate)
        with self._connections_lock:
            connection = self._connections.get(connection_key)
            if connection is None:
                connection = _SharedSerialConnection(
                    serial.Serial(
                        port,
                        self.settings.baud_rate,
                        timeout=_SERIAL_READ_TIMEOUT_S,
                    )
                )
                self._connections[connection_key] = connection
            connection.users += 1
            self._connection = connection
            self._connection_key = connection_key

        self.started_at = time.monotonic()
        self._open_log()
        if connection.users == 1:
            time.sleep(2.0) # TODO: check sleep time
            with connection.lock:
                connection.serial.reset_input_buffer()
        self._log(f"Connected to {port} at {self.settings.baud_rate} baud")

    def close(self):
        connection = self._connection
        if connection is not None:
            with self._connections_lock:
                connection.users -= 1
                if connection.users == 0:
                    with connection.lock:
                        connection.serial.close()
                    self._connections.pop(self._connection_key, None)
            self._connection = None
            self._connection_key = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def start(self):
        self._write("G\n")

    def stop(self):
        self._write("S\n")

    def set_ramp_rate(self, degc_per_min: float):
        self._write(f"R{degc_per_min:.2f}\n")

    def set_target(self, degc: float):
        self._write(f"P{degc:.2f}\n")

    def poll_status(self) -> TemperatureStatus | None:
        # The firmware already emits a complete status line once per second.
        # Requesting T here adds an extra non-status reply and can starve control commands.
        return self.read_status()

    def read_status(self) -> TemperatureStatus | None:
        connection = self._connection
        if connection is None:
            raise RuntimeError("Temperature controller is not connected.")

        with connection.lock:
            raw = connection.serial.readline()
        if not raw:
            return None

        line = raw.decode("utf-8", errors="ignore").strip()
        if not line:
            return None

        self._log(line)
        return self._parse_status(line)

    def _write(self, command: str):
        connection = self._connection
        if connection is None:
            raise RuntimeError("Temperature controller is not connected.")
        with connection.lock:
            connection.serial.write(command.encode("ascii"))
        self._log(f"Sent command: {command.strip()}")

    def _parse_status(self, line: str) -> TemperatureStatus | None:
        temperature = self._field_float(line, "T")
        setpoint = self._field_float(line, "SP")
        if temperature is None or setpoint is None:
            return None

        mode_match = _MODE_RE.search(line)
        return TemperatureStatus(
            elapsed_s=self._elapsed_s(),
            mode=mode_match.group("mode").upper() if mode_match else None,
            temperature_c=temperature,
            setpoint_c=setpoint,
            active_setpoint_c=self._field_float(line, "ActiveSP"),
            feedforward=self._field_float(line, "FF"),
            integral=self._field_float(line, "I"),
            pid=self._field_float(line, "PID"),
            pwm=self._field_int(line, "PWM"),
            raw_line=line,
        )

    def progress_message(
        self,
        status: TemperatureStatus,
        target_c: float,
        wait_elapsed_s: float,
        wait_s: float,
        timer_starts_immediately: bool,
    ) -> str:
        mode = f"{status.mode} " if status.mode else ""
        timer_label = "elapsed" if timer_starts_immediately else "stable"
        return (
            f"{mode}{status.temperature_c:.2f}/{target_c:.2f} C "
            f"({timer_label} {wait_elapsed_s:.0f}/{wait_s:.0f} s)"
        )

    def _open_log(self):
        if not self.settings.log_dir:
            return

        log_dir = Path(self.settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = log_dir / f"SetTempWatcher_{timestamp}.txt"
        self._log_handle = self.log_path.open("w", encoding="utf-8")

    def _log(self, message: str):
        if self._log_handle is None:
            return

        elapsed_s = self._elapsed_s()
        self._log_handle.write(f"{datetime.now():%H:%M:%S}  [+{elapsed_s:6.0f}s] {message}\n")
        self._log_handle.flush()

    def _elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    @staticmethod
    def _field_float(line: str, field: str) -> float | None:
        match = re.search(rf"\b{re.escape(field)}:(-?\d+(?:\.\d+)?)", line)
        return float(match.group(1)) if match else None

    @staticmethod
    def _field_int(line: str, field: str) -> int | None:
        match = re.search(rf"\b{re.escape(field)}:(-?\d+)", line)
        return int(match.group(1)) if match else None

    @staticmethod
    def find_arduino_port() -> str | None:
        if serial is None:
            return None

        for port in serial.tools.list_ports.comports():
            description = port.description or ""
            if any(token in description for token in ("Arduino", "CH340", "USB Serial")):
                return port.device
        return None
 
