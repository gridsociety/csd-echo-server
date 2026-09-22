"""Configuration loading: TOML file plus command line overrides."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_WELCOME = "This is a CSD echo server."

#: What call.voice_calls accepts.
VOICE_CALL_ACTIONS = ("accept", "reject", "ignore")

#: Commands sent after the modem-level options, before the server starts listening.
DEFAULT_EXTRA_INIT: tuple[str, ...] = ()


class ConfigError(Exception):
    """Raised when a configuration file is missing keys or has the wrong shape."""


@dataclass(slots=True)
class SerialConfig:
    """Settings for the serial line between the host and the modem."""

    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    rtscts: bool = False


@dataclass(slots=True)
class ModemConfig:
    """Settings for how the modem is put into auto-answer mode."""

    #: Argument of AT+CBST, selecting the CSD bearer service (speed, name, connection element).
    bearer: str | None = "7,0,1"
    #: Argument of AT+CSNS. Tells the modem how to treat calls that carry no bearer
    #: element, which is what a SIM without a separate data number will produce.
    #: Use -1 to leave the modem's own setting alone.
    single_numbering: int = 4
    #: Send AT+IPR=<serial baudrate>. Modems left on autobauding resynchronise on
    #: every command and lose some of them outright, which is worth avoiding.
    fix_bit_rate: bool = True
    #: Ask for the caller number (AT+CLIP=1) and the extended ring report (AT+CRC=1).
    caller_id: bool = True
    extended_ring: bool = True
    #: Extra AT commands appended to the init sequence.
    init_commands: list[str] = field(default_factory=lambda: list(DEFAULT_EXTRA_INIT))
    #: Seconds to wait before sending ATA, and how long to wait for CONNECT afterwards.
    answer_delay: float = 0.5
    connect_timeout: float = 60.0
    #: Seconds to wait for the modem to become reachable again after a failure.
    retry_delay: float = 5.0


@dataclass(slots=True)
class CallConfig:
    """Which calls are answered, and the limits applied once they are."""

    #: What to do with a call the network signalled as voice or fax.
    #:
    #: "accept"  answer it like any other call, the way industrial CSD modems do
    #:           and the only thing that works on a SIM that signals data calls
    #:           as voice. A call that really is a voice call comes up without a
    #:           data carrier and is hung up again.
    #: "reject"  hang up at once, so the caller hears the line as unavailable.
    #: "ignore"  neither answer nor hang up: the caller keeps hearing the
    #:           ringback and reaches voicemail, as on a module that is simply
    #:           not answering.
    voice_calls: str = "accept"
    #: Hang up after this many seconds without data from the caller (0 disables).
    idle_timeout: float = 300.0
    #: Hang up after this many seconds regardless of activity (0 disables).
    max_duration: float = 3600.0


@dataclass(slots=True)
class EchoConfig:
    """What the caller sees once the call is up."""

    welcome: str = DEFAULT_WELCOME
    #: Include the call/protocol summary after the welcome message.
    show_call_info: bool = True
    #: "crlf" turns every incoming CR or LF into CRLF, which is what terminal
    #: programs expect; "raw" echoes the bytes exactly as received.
    newline: str = "crlf"


@dataclass(slots=True)
class Config:
    """Full server configuration."""

    serial: SerialConfig = field(default_factory=SerialConfig)
    modem: ModemConfig = field(default_factory=ModemConfig)
    call: CallConfig = field(default_factory=CallConfig)
    echo: EchoConfig = field(default_factory=EchoConfig)

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        """Build a configuration from *path*, falling back to the defaults."""
        if path is None:
            return cls()
        file = Path(path)
        try:
            raw = tomllib.loads(file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read {file}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {file}: {exc}") from exc
        return cls.from_mapping(raw, source=str(file))

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], source: str = "configuration") -> Config:
        """Build a configuration from an already parsed mapping."""
        sections = {f.name: f.type for f in fields(cls)}
        unknown = set(raw) - set(sections)
        if unknown:
            raise ConfigError(f"unknown section(s) in {source}: {', '.join(sorted(unknown))}")
        config = cls()
        for name in sections:
            values = raw.get(name)
            if values is None:
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"section [{name}] in {source} must be a table")
            section = getattr(config, name)
            allowed = {f.name for f in fields(section)}
            extra = set(values) - allowed
            if extra:
                raise ConfigError(
                    f"unknown key(s) in [{name}] of {source}: {', '.join(sorted(extra))}"
                )
            for key, value in values.items():
                setattr(section, key, value)
        config.validate()
        return config

    def validate(self) -> None:
        """Check the values that would otherwise fail much later, at call time."""
        if self.echo.newline not in ("crlf", "raw"):
            raise ConfigError("echo.newline must be 'crlf' or 'raw'")
        if self.serial.baudrate <= 0:
            raise ConfigError("serial.baudrate must be positive")
        if self.modem.single_numbering not in (-1, 0, 2, 4):
            raise ConfigError("modem.single_numbering must be -1 (unset), 0, 2 or 4")
        if self.call.voice_calls not in VOICE_CALL_ACTIONS:
            raise ConfigError(
                f"call.voice_calls must be one of {', '.join(sorted(VOICE_CALL_ACTIONS))}"
            )
        if self.call.idle_timeout < 0 or self.call.max_duration < 0:
            raise ConfigError("call timeouts cannot be negative")
