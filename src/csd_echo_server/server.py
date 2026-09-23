"""The answer-and-echo loop."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import UTC, datetime

import serial

from csd_echo_server.config import Config
from csd_echo_server.modem import (
    IncomingCall,
    Modem,
    ModemError,
    NoDataCarrier,
)

log = logging.getLogger(__name__)

#: Emitted by the modem on the data line when the remote end hangs up.
NO_CARRIER = b"NO CARRIER"

#: A ring report arrives every few seconds, so this much silence means the
#: caller has given up; RING_PATIENCE caps how long one ignored call can last.
RING_GAP = 8.0
RING_PATIENCE = 180.0


def held_back(data: bytes) -> int:
    """Length of the trailing bytes of *data* that could start a NO CARRIER report.

    They are kept out of the echo until the next read settles it, so that a
    report split across two reads is never echoed back to the caller.
    """
    for size in range(min(len(NO_CARRIER) - 1, len(data)), 0, -1):
        if data.endswith(NO_CARRIER[:size]):
            return size
    return 0


def to_crlf(data: bytes) -> bytes:
    """Normalise any line ending to CRLF, which is what terminal callers expect."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")


def render_banner(
    config: Config,
    call: IncomingCall,
    call_number: int,
    now: datetime | None = None,
) -> str:
    """Report observed call details, never configured modem or server settings."""
    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [config.echo.welcome.rstrip("\n")]
    if config.echo.show_call_info:
        info = [("Call", f"#{call_number} at {timestamp}")]
        for label, value in (
            ("Caller", call.caller),
            ("Call type", call.bearer),
            ("Connection", call.connect),
        ):
            if value:
                info.append((label, value))
        width = max(len(label) for label, _ in info)
        lines.append("")
        lines.extend(f"{label.ljust(width)} : {value}" for label, value in info)
    lines.extend(["", "Everything you send is echoed back. Hang up to end the call.", ""])
    return "\n".join(lines) + "\n"


class EchoServer:
    """Owns the modem, answers every incoming data call and echoes the traffic."""

    def __init__(self, config: Config, stop: threading.Event | None = None) -> None:
        self.config = config
        self.stop = stop or threading.Event()
        self.modem = Modem(config.serial, config.modem)
        self.calls_answered = 0

    def run(self) -> int:
        """Serve calls until the stop event is set. Returns a process exit code."""
        while not self.stop.is_set():
            try:
                self._open()
                self._serve()
            except (ModemError, serial.SerialException) as exc:
                if self.stop.is_set():
                    break
                log.error("modem error: %s", exc)
                self.modem.close()
                log.info("retrying in %.0f s", self.config.modem.retry_delay)
                self.stop.wait(self.config.modem.retry_delay)
        self.modem.close()
        log.info("stopped after %d call(s)", self.calls_answered)
        return 0

    def _open(self) -> None:
        if self.modem.port is not None:
            return
        log.info("opening %s at %d baud", self.config.serial.port, self.config.serial.baudrate)
        self.modem.open()
        log.info("modem ready: %s", self.modem.identify().replace("\n", " "))
        log.info("waiting for incoming CSD calls")

    def _serve(self) -> None:
        while not self.stop.is_set():
            call = self.modem.wait_for_call(timeout=1.0)
            if call is None:
                continue
            log.info("incoming call: caller=%s type=%s", call.caller or "unknown",
                     call.bearer or "not signalled")
            action = self.config.call.voice_calls
            if action != "accept" and not call.is_data:
                if action == "ignore":
                    log.info("call signalled as %s, letting it ring", call.bearer)
                    self._let_it_ring()
                else:
                    log.info("call signalled as %s, rejecting", call.bearer)
                    self.modem.hangup()
                continue
            try:
                self.modem.answer(call)
            except NoDataCarrier as exc:
                log.info("%s", exc)
                self.modem.hangup()
                continue
            self.calls_answered += 1
            log.info("answered call #%d: %s", self.calls_answered, call.connect)
            try:
                self._echo(call)
            finally:
                self.modem.hangup()
                log.info("call #%d ended, waiting for incoming CSD calls", self.calls_answered)

    def _let_it_ring(self) -> None:
        """Consume the ring reports of a call we neither answer nor hang up on.

        Hanging up would tell the caller the line is busy; staying quiet lets the
        network run the call out to voicemail on its own.
        """
        deadline = time.monotonic() + RING_PATIENCE
        while not self.stop.is_set() and time.monotonic() < deadline:
            line = self.modem.read_line(timeout=RING_GAP)
            if line is None:  # the ringing stopped
                return
            log.debug("ignoring %s", line)
            if line.startswith("NO CARRIER"):
                return

    def _echo(self, call: IncomingCall) -> None:
        """Send the banner, then echo everything until the call ends."""
        banner = render_banner(self.config, call, self.calls_answered)
        self.modem.write_data(to_crlf(banner.encode("utf-8")))

        crlf = self.config.echo.newline == "crlf"
        started = time.monotonic()
        last_data = started
        echoed = 0
        # DCD only tells us anything if the cable carries it; a serial adapter
        # wired for three signals reports a carrier that is always low.
        use_dcd = self.modem.carrier
        if not use_dcd:
            log.debug("DCD is low right after CONNECT, relying on NO CARRIER instead")
        pending = b""

        while not self.stop.is_set():
            data = self.modem.read_data(timeout=0.2)
            now = time.monotonic()
            if data:
                buffer = pending + data
                index = buffer.find(NO_CARRIER)
                if index >= 0:
                    self._send(buffer[:index], crlf)
                    log.info("caller hung up (NO CARRIER) after %d byte(s)", echoed)
                    return
                hold = held_back(buffer)
                send, pending = buffer[: len(buffer) - hold], buffer[len(buffer) - hold :]
                echoed += len(data)
                last_data = now
                self._send(send, crlf)
            elif use_dcd and not self.modem.carrier:
                log.info("carrier lost after %d byte(s)", echoed)
                return

            idle = self.config.call.idle_timeout
            if idle > 0 and now - last_data > idle:
                log.info("idle for %.0f s, hanging up", idle)
                self._goodbye("Idle timeout reached, hanging up.")
                return
            limit = self.config.call.max_duration
            if limit > 0 and now - started > limit:
                log.info("call limit of %.0f s reached, hanging up", limit)
                self._goodbye("Call time limit reached, hanging up.")
                return

    def _send(self, data: bytes, crlf: bool) -> None:
        if data:
            self.modem.write_data(to_crlf(data) if crlf else data)

    def _goodbye(self, message: str) -> None:
        with contextlib.suppress(ModemError, serial.SerialException):
            self.modem.write_data(to_crlf(f"\n{message}\n".encode()))
