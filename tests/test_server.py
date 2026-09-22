import threading

from csd_echo_server.config import Config
from csd_echo_server.modem import IncomingCall
from csd_echo_server.server import EchoServer, held_back, render_banner, to_crlf


class FakeModem:
    """Stands in for a connected modem in data mode."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = bytearray()
        self.carrier = True
        self.hung_up = False
        self.port = object()

    def read_data(self, timeout):
        return self.chunks.pop(0) if self.chunks else b""

    def write_data(self, data):
        self.sent.extend(data)

    def hangup(self):
        self.hung_up = True


def run_echo(chunks, config=None):
    server = EchoServer(config or Config(), threading.Event())
    server.modem = FakeModem(chunks)
    server.calls_answered = 1
    server._echo(IncomingCall(caller="+390123456789", bearer="REL ASYNC",
                              connect="CONNECT 9600/RLP"))
    return server.modem


def test_held_back_keeps_a_partial_no_carrier_report():
    assert held_back(b"hello") == 0
    assert held_back(b"hi\r\nNO CA") == 5
    assert held_back(b"N") == 1
    assert held_back(b"NO CARRIE") == 9


def test_to_crlf_normalises_every_line_ending():
    assert to_crlf(b"a\nb\r\nc\rd") == b"a\r\nb\r\nc\r\nd"


def test_banner_contains_the_welcome_and_the_call_details():
    config = Config()
    config.echo.welcome = "Welcome aboard."
    banner = render_banner(config, IncomingCall(caller="+390123456789", bearer="REL ASYNC",
                                                connect="CONNECT 9600/RLP"), call_number=3)
    assert banner.startswith("Welcome aboard.")
    assert "+390123456789" in banner
    assert "REL ASYNC" in banner
    assert "CONNECT 9600/RLP" in banner
    assert "9600 bps V.32, non-transparent" in banner
    assert "#3" in banner


def test_banner_can_be_just_the_welcome_message():
    config = Config()
    config.echo.show_call_info = False
    banner = render_banner(config, IncomingCall(), call_number=1)
    assert "Caller" not in banner
    assert banner.startswith("This is a CSD echo server.")


def test_banner_names_what_the_network_did_not_signal():
    banner = render_banner(Config(), IncomingCall(), call_number=1)
    assert "withheld or not signalled" in banner


def test_echo_sends_the_banner_then_repeats_the_input():
    modem = run_echo([b"hello", b"\r\nNO CARRIER\r\n"])
    banner, _, echoed = modem.sent.partition(b"Hang up to end the call.\r\n\r\n")
    assert b"This is a CSD echo server." in banner
    assert echoed == b"hello\r\n"  # the CRLF the modem sends ahead of NO CARRIER


def test_echo_stops_at_no_carrier_without_echoing_it():
    modem = run_echo([b"abc\r\nNO CARRIER\r\n"])
    assert b"NO CARRIER" not in modem.sent
    assert modem.sent.endswith(b"abc\r\n")


def test_no_carrier_is_detected_across_reads():
    modem = run_echo([b"hi\r\nNO CA", b"RRIER\r\n"])
    assert b"NO CARRIER" not in modem.sent
    assert modem.sent.endswith(b"hi\r\n")


def test_echo_honours_raw_newline_mode():
    config = Config()
    config.echo.newline = "raw"
    modem = run_echo([b"a\nb", b"NO CARRIER"], config)
    assert modem.sent.endswith(b"a\nb")


def test_idle_timeout_ends_the_call():
    config = Config()
    config.call.idle_timeout = 0.05
    modem = run_echo([], config)
    assert b"Idle timeout reached" in modem.sent


def test_max_duration_ends_the_call():
    config = Config()
    config.call.idle_timeout = 0
    config.call.max_duration = 0.05
    modem = run_echo([], config)
    assert b"Call time limit reached" in modem.sent
