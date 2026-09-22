"""End to end test of the server against a modem simulated on a pty."""

from __future__ import annotations

import os
import pty
import select
import termios
import threading
import time

import pytest

from csd_echo_server.config import Config
from csd_echo_server.server import EchoServer

TIMEOUT = 15.0


class FakeModem:
    """Answers AT commands on the master side of a pty, then carries data."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.data_mode = False
        self.commands: list[str] = []
        self.received = bytearray()
        self._pending = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def _run(self) -> None:
        while not self._stop.is_set():
            readable, _, _ = select.select([self.fd], [], [], 0.05)
            if not readable:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return
            if self.data_mode:
                self.received.extend(chunk)
                continue
            self._pending.extend(chunk)
            while b"\r" in self._pending:
                line, _, rest = bytes(self._pending).partition(b"\r")
                self._pending = bytearray(rest)
                self._handle(line.decode("ascii", "replace").strip())

    def _handle(self, command: str) -> None:
        if not command:
            return
        self.commands.append(command)
        if command == "ATA":
            self.send(b"\r\nCONNECT 9600/RLP\r\n")
            self.data_mode = True
        elif command == "ATI":
            self.send(b"\r\nFakeCo FM-1\r\nOK\r\n")
        else:
            self.send(b"\r\nOK\r\n")


def wait_until(predicate, message: str, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


@pytest.fixture
def modem_and_server():
    master, slave = pty.openpty()
    for fd in (master, slave):
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # lflag
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    config = Config()
    config.serial.port = os.ttyname(slave)
    config.modem.answer_delay = 0.2
    config.call.idle_timeout = 0
    config.call.max_duration = 0

    modem = FakeModem(master)
    modem.start()
    stop = threading.Event()
    server = EchoServer(config, stop)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield modem, server
    finally:
        stop.set()
        thread.join(timeout=10)
        modem.stop()
        os.close(master)
        os.close(slave)


def test_server_answers_a_call_and_echoes_the_traffic(modem_and_server):
    modem, server = modem_and_server
    wait_until(lambda: "ATI" in modem.commands, "the init sequence")
    assert "AT+CSNS=4" in modem.commands
    assert "ATS0=0" in modem.commands

    modem.send(b'\r\n+CRING: REL ASYNC\r\n+CLIP: "+390123456789",145,,,,0\r\n')
    wait_until(lambda: "ATA" in modem.commands, "the call to be answered")

    wait_until(lambda: b"Hang up to end the call." in modem.received, "the banner")
    banner = bytes(modem.received)
    assert b"This is a CSD echo server." in banner
    assert b"+390123456789" in banner
    assert b"REL ASYNC" in banner
    assert b"CONNECT 9600/RLP" in banner
    assert b"\r\n" in banner

    modem.received.clear()
    modem.send(b"ping\r")
    wait_until(lambda: bytes(modem.received) == b"ping\r\n", "the echo")

    modem.received.clear()
    modem.data_mode = False
    modem.send(b"\r\nNO CARRIER\r\n")
    wait_until(lambda: modem.commands.count("ATH") == 1, "the modem to be hung up")
    assert server.calls_answered == 1
    assert b"NO CARRIER" not in bytes(modem.received)

    # And the server goes back to waiting for the next call.
    modem.commands.clear()
    modem.data_mode = False
    modem.send(b"\r\n+CRING: REL ASYNC\r\n")
    wait_until(lambda: "ATA" in modem.commands, "the next call to be answered")
    wait_until(lambda: server.calls_answered == 2, "the second call to be counted")


def test_voice_calls_can_be_rejected(modem_and_server):
    modem, server = modem_and_server
    wait_until(lambda: "ATI" in modem.commands, "the init sequence")
    server.config.call.reject_voice_calls = True

    modem.commands.clear()
    modem.send(b"\r\n+CRING: VOICE\r\n")
    wait_until(lambda: "ATH" in modem.commands, "the call to be rejected")
    assert "ATA" not in modem.commands
    assert server.calls_answered == 0
