"""A thin AT-command layer over a serial modem that can take CSD calls."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field

import serial

from csd_echo_server.config import ModemConfig, SerialConfig

log = logging.getLogger(__name__)

#: Final result codes that end a command, per ITU-T V.250 and 3GPP TS 27.007.
_FINAL_OK = ("OK", "CONNECT")
_FINAL_ERROR = ("ERROR", "NO CARRIER", "NO DIALTONE", "BUSY", "NO ANSWER")

#: Speeds of AT+CBST, from 3GPP TS 27.007 section 6.7, for the banner shown to the caller.
BEARER_SPEEDS = {
    0: "autobauding",
    4: "2400 bps V.22bis",
    6: "4800 bps V.32",
    7: "9600 bps V.32",
    12: "9600 bps V.34",
    14: "14400 bps V.34",
    68: "2400 bps V.110",
    70: "4800 bps V.110",
    71: "9600 bps V.110",
    75: "14400 bps V.110",
}
BEARER_ELEMENTS = {0: "transparent", 1: "non-transparent", 2: "both, transparent preferred",
                   3: "both, non-transparent preferred"}


class ModemError(Exception):
    """The modem did not answer, or answered with an error result code."""


class NoDataCarrier(ModemError):
    """The call was answered but the modem never handed over a data stream.

    A modem answering a call the network signalled as voice reports a plain OK
    instead of CONNECT: the call is up, but there is nothing to echo.
    """


@dataclass(slots=True)
class IncomingCall:
    """What the modem told us about a call before we answered it."""

    caller: str | None = None
    #: Bearer reported by +CRING, e.g. "REL ASYNC" for a non-transparent data call.
    bearer: str | None = None
    #: Result code returned by ATA, e.g. "CONNECT 9600/RLP".
    connect: str | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def is_data(self) -> bool:
        """True unless the modem explicitly told us this is a voice or fax call."""
        if self.bearer is None:
            return True
        bearer = self.bearer.upper()
        return not (bearer.startswith("VOICE") or bearer.startswith("FAX"))


class Modem:
    """Serial modem in command mode, able to answer a call and hand over the data stream."""

    def __init__(self, serial_config: SerialConfig, modem_config: ModemConfig) -> None:
        self.serial_config = serial_config
        self.config = modem_config
        self.port: serial.Serial | None = None
        self._buffer = bytearray()
        #: False on a port that cannot drive DTR, such as a three-wire cable.
        self._modem_control = True

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the serial port and run the init sequence."""
        self.port = serial.Serial(
            port=self.serial_config.port,
            baudrate=self.serial_config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=self.serial_config.rtscts,
            timeout=0.2,
            write_timeout=5.0,
        )
        self._modem_control = self._set_line(dtr=True, rts=True)
        if not self._modem_control:
            log.debug("serial port does not support the modem control lines")
        self._buffer.clear()
        self.port.reset_input_buffer()
        self.initialise()

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            finally:
                self.port = None

    def _require_port(self) -> serial.Serial:
        if self.port is None:
            raise ModemError("serial port is not open")
        return self.port

    # -- init --------------------------------------------------------------

    def init_sequence(self) -> list[str]:
        """The AT commands used to bring the modem into auto-answer state."""
        cfg = self.config
        commands = [
            "ATZ",       # reset to the stored profile
            "ATE0",      # no command echo, so responses are easy to parse
        ]
        if cfg.fix_bit_rate:
            commands.append(f"AT+IPR={self.serial_config.baudrate}")
        commands += [
            "AT+CMEE=2", # verbose error messages
            "AT&C1",     # DCD follows the data carrier
            "AT&D2",     # dropping DTR hangs up the call
            "ATS0=0",    # never answer by itself: we answer with ATA
        ]
        if cfg.extended_ring:
            commands.append("AT+CRC=1")
        if cfg.caller_id:
            commands.append("AT+CLIP=1")
        if cfg.single_numbering >= 0:
            commands.append(f"AT+CSNS={cfg.single_numbering}")
        if cfg.bearer:
            commands.append(f"AT+CBST={cfg.bearer}")
        commands.extend(cfg.init_commands)
        return commands

    def initialise(self) -> None:
        """Run the init sequence, tolerating options the modem does not implement."""
        port = self._require_port()
        port.reset_input_buffer()
        self._buffer.clear()
        # ATZ may be answered by an echo of the command itself while ATE0 is still pending.
        for attempt in range(3):
            try:
                self.command("AT", timeout=2.0)
                break
            except ModemError:
                if attempt == 2:
                    raise
                time.sleep(0.5)
        for command in self.init_sequence():
            # A modem still on autobauding drops the odd command while it
            # resynchronises, so one lost reply is worth a second try.
            for attempt in (1, 2):
                try:
                    self.command(command, timeout=5.0)
                    break
                except ModemError as exc:
                    if attempt == 2:
                        log.warning("init command %s failed: %s", command, exc)

    def identify(self) -> str:
        """Return the manufacturer and model, for the log and the caller banner."""
        try:
            lines = self.command("ATI", timeout=3.0)
        except ModemError:
            return "unknown modem"
        parts = [line for line in lines if line and not line.startswith(("OK", "AT"))]
        return " ".join(parts) if parts else "unknown modem"

    # -- low level line I/O ------------------------------------------------

    def read_line(self, timeout: float) -> str | None:
        """Read one non-empty line, or None when *timeout* expires."""
        port = self._require_port()
        deadline = time.monotonic() + timeout
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    return line
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            port.timeout = min(0.2, remaining)
            chunk = port.read(max(1, port.in_waiting))
            if chunk:
                self._buffer.extend(chunk)

    def write_command(self, command: str) -> None:
        port = self._require_port()
        log.debug(">> %s", command)
        port.write(command.encode("ascii") + b"\r")
        port.flush()

    def command(self, command: str, timeout: float = 5.0) -> list[str]:
        """Send *command* and collect its response up to the final result code."""
        self.write_command(command)
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemError(f"timed out waiting for a reply to {command}")
            line = self.read_line(remaining)
            if line is None:
                continue
            log.debug("<< %s", line)
            if line == command:  # echo, in case ATE0 has not taken effect yet
                continue
            lines.append(line)
            if line.startswith(_FINAL_OK):
                return lines
            if line.startswith(_FINAL_ERROR):
                raise ModemError(f"{command} -> {line}")

    # -- calls -------------------------------------------------------------

    def wait_for_call(self, timeout: float) -> IncomingCall | None:
        """Block until the modem reports a call, or *timeout* seconds elapse."""
        deadline = time.monotonic() + timeout
        call: IncomingCall | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            line = self.read_line(remaining)
            if line is None:
                continue
            log.debug("<< %s", line)
            if line.startswith(("RING", "+CRING:")):
                if call is None:
                    call = IncomingCall()
                call.lines.append(line)
                if line.startswith("+CRING:"):
                    call.bearer = line.split(":", 1)[1].strip()
                # Give the network a moment to deliver +CLIP for this ring.
                self._collect_call_details(call, self.config.answer_delay)
                return call
            if line.startswith("+CLIP:") and call is not None:
                call.caller = _parse_clip(line)

    def _collect_call_details(self, call: IncomingCall, window: float) -> None:
        """Read the lines that follow a RING, which is where +CLIP arrives."""
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            line = self.read_line(remaining)
            if line is None:
                continue
            log.debug("<< %s", line)
            call.lines.append(line)
            if line.startswith("+CLIP:"):
                call.caller = _parse_clip(line)
            elif line.startswith("+CRING:") and call.bearer is None:
                call.bearer = line.split(":", 1)[1].strip()
            elif line.startswith("NO CARRIER"):
                raise ModemError("caller hung up before we answered")

    def answer(self, call: IncomingCall) -> IncomingCall:
        """Send ATA and wait for the CONNECT result code."""
        self.write_command("ATA")
        deadline = time.monotonic() + self.config.connect_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemError("timed out waiting for CONNECT")
            line = self.read_line(remaining)
            if line is None:
                continue
            log.debug("<< %s", line)
            if line in ("ATA", "RING") or line.startswith("+CRING:"):
                continue
            if line.startswith("+CLIP:"):
                call.caller = _parse_clip(line)
                continue
            if line.startswith("CONNECT"):
                call.connect = line
                return call
            if line == "OK":
                raise NoDataCarrier("call answered without a data carrier, hanging up")
            if line.startswith(_FINAL_ERROR):
                raise ModemError(f"ATA -> {line}")

    def _set_line(self, *, dtr: bool | None = None, rts: bool | None = None) -> bool:
        """Drive DTR or RTS, reporting whether the port supports it at all."""
        port = self._require_port()
        try:
            if dtr is not None:
                port.dtr = dtr
            if rts is not None:
                port.rts = rts
        except (OSError, serial.SerialException):
            return False
        return True

    def hangup(self) -> None:
        """Return the modem to command mode, whatever state the call is in."""
        port = self.port
        if port is None:
            return
        try:
            if self._modem_control:
                # AT&D2 makes the modem drop the call when DTR goes low. This
                # works in data mode, where ATH would just be sent to the caller.
                self._set_line(dtr=False)
                time.sleep(1.0)
                self._set_line(dtr=True)
                time.sleep(0.5)
            port.reset_input_buffer()
            self._buffer.clear()
            if not self._in_command_mode():
                # Without DTR the only way back is the escape sequence, which
                # needs a second of silence on either side to be recognised.
                log.debug("still in data mode, sending the escape sequence")
                time.sleep(1.0)
                port.write(b"+++")
                port.flush()
                time.sleep(1.0)
                self._in_command_mode()
            with contextlib.suppress(ModemError):
                self.command("ATH", timeout=3.0)
            self.command("AT", timeout=3.0)
        except (serial.SerialException, ModemError) as exc:
            raise ModemError(f"could not return to command mode: {exc}") from exc

    def _in_command_mode(self) -> bool:
        try:
            self.command("AT", timeout=2.0)
        except ModemError:
            return False
        return True

    # -- data mode ---------------------------------------------------------

    @property
    def carrier(self) -> bool:
        """State of the DCD line, which is only meaningful on a fully wired cable."""
        port = self._require_port()
        try:
            return bool(port.cd)
        except (OSError, serial.SerialException):
            return False

    def read_data(self, timeout: float) -> bytes:
        """Read whatever the caller sent, waiting at most *timeout* seconds."""
        port = self._require_port()
        if self._buffer:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        port.timeout = timeout
        first = port.read(1)
        if not first:
            return b""
        rest = port.read(port.in_waiting) if port.in_waiting else b""
        return first + rest

    def write_data(self, data: bytes) -> None:
        port = self._require_port()
        port.write(data)
        port.flush()


def _parse_clip(line: str) -> str | None:
    """Pull the number out of `+CLIP: "+39...",145,...`."""
    _, _, rest = line.partition(":")
    parts = [part.strip() for part in rest.split(",")]
    if not parts:
        return None
    number = parts[0].strip('"').strip()
    return number or None


def describe_bearer(bearer: str | None) -> str:
    """Render an AT+CBST argument as something a caller can read."""
    if not bearer:
        return "modem default"
    parts = [part.strip() for part in bearer.split(",")]
    try:
        speed = int(parts[0])
    except (ValueError, IndexError):
        return bearer
    description = BEARER_SPEEDS.get(speed, f"speed code {speed}")
    if len(parts) >= 3:
        try:
            element = BEARER_ELEMENTS.get(int(parts[2]))
        except ValueError:
            element = None
        if element:
            description = f"{description}, {element}"
    return description
