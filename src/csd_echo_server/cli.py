"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from types import FrameType

import serial

from csd_echo_server import __version__
from csd_echo_server.config import Config, ConfigError
from csd_echo_server.modem import Modem, ModemError
from csd_echo_server.server import EchoServer

DEFAULT_CONFIG_PATH = "/etc/csd-echo-server.toml"

log = logging.getLogger("csd_echo_server")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csd-echo-server",
        description="Answer GSM circuit-switched data calls and echo back what the caller sends.",
    )
    parser.add_argument(
        "-c", "--config", metavar="FILE",
        help=f"configuration file (default: {DEFAULT_CONFIG_PATH} when it exists)",
    )
    parser.add_argument("-p", "--port", metavar="DEV", help="serial port of the modem")
    parser.add_argument("-b", "--baud", type=int, metavar="RATE", help="serial port speed")
    parser.add_argument("-w", "--welcome", metavar="TEXT", help="welcome message sent to callers")
    parser.add_argument(
        "--check", action="store_true",
        help="open the modem, run the init sequence, report what it says and exit",
    )
    parser.add_argument(
        "-l", "--log-level", default=os.environ.get("CSD_ECHO_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (DEBUG also logs every AT command)",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def load_config(args: argparse.Namespace) -> Config:
    path = args.config
    if path is None and os.path.exists(DEFAULT_CONFIG_PATH):
        path = DEFAULT_CONFIG_PATH
    config = Config.load(path)
    if args.port:
        config.serial.port = args.port
    if args.baud:
        config.serial.baudrate = args.baud
    if args.welcome:
        config.echo.welcome = args.welcome
    config.validate()
    if path:
        log.info("configuration loaded from %s", path)
    return config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )


def check(config: Config) -> int:
    """Open the modem once, report its identity and settings, then exit."""
    modem = Modem(config.serial, config.modem)
    modem.open()
    try:
        print(f"port            : {config.serial.port} at {config.serial.baudrate} baud")
        print(f"modem           : {modem.identify()}")
        for query, label in (
            ("AT+CPIN?", "SIM"),
            ("AT+CREG?", "registration"),
            ("AT+CSQ", "signal"),
            ("AT+CBST?", "bearer"),
        ):
            try:
                answer = [line for line in modem.command(query) if line != "OK"]
            except ModemError as exc:
                answer = [str(exc)]
            print(f"{label:<16}: {' '.join(answer) or 'no answer'}")
    finally:
        modem.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        config = load_config(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    try:
        if args.check:
            return check(config)
        stop = threading.Event()

        def handle_signal(signum: int, _frame: FrameType | None) -> None:
            log.info("received %s, shutting down", signal.Signals(signum).name)
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        return EchoServer(config, stop).run()
    except (ModemError, serial.SerialException) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
