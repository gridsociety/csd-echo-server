import pytest

from csd_echo_server.config import ModemConfig, SerialConfig
from csd_echo_server.modem import (
    IncomingCall,
    Modem,
    ModemError,
    NoDataCarrier,
    _parse_clip,
    describe_bearer,
)


class FakePort:
    """Just enough of serial.Serial to drive the line reader."""

    def __init__(self, responses=b""):
        self.incoming = bytearray(responses)
        self.written = bytearray()
        self.timeout = 0.2
        self.cd = True

    @property
    def in_waiting(self):
        return len(self.incoming)

    def read(self, size=1):
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


def make_modem(responses=b"", **modem_kwargs):
    modem = Modem(SerialConfig(), ModemConfig(**modem_kwargs))
    modem.port = FakePort(responses)
    return modem


def test_command_collects_lines_until_ok():
    modem = make_modem(b"\r\nCinterion\r\nMC55i\r\nOK\r\n")
    assert modem.command("ATI") == ["Cinterion", "MC55i", "OK"]
    assert modem.port.written == b"ATI\r"


def test_command_raises_on_error_result_code():
    modem = make_modem(b"\r\n+CME ERROR: 3\r\nERROR\r\n")
    with pytest.raises(ModemError):
        modem.command("AT+CBST=99,0,1")


def test_command_times_out_without_a_reply():
    modem = make_modem(b"")
    with pytest.raises(ModemError, match="timed out"):
        modem.command("AT", timeout=0.1)


def test_command_skips_the_echoed_command():
    modem = make_modem(b"AT+CSQ\r\n+CSQ: 9,99\r\nOK\r\n")
    assert modem.command("AT+CSQ") == ["+CSQ: 9,99", "OK"]


def test_wait_for_call_reads_ring_and_caller_id():
    modem = make_modem(b'\r\n+CRING: REL ASYNC\r\n+CLIP: "+390123456789",145,,,,0\r\n',
                       answer_delay=0.05)
    call = modem.wait_for_call(timeout=1.0)
    assert call is not None
    assert call.bearer == "REL ASYNC"
    assert call.caller == "+390123456789"
    assert call.is_data


def test_wait_for_call_returns_none_when_nothing_rings():
    assert make_modem(b"").wait_for_call(timeout=0.1) is None


def test_answer_returns_the_connect_result_code():
    modem = make_modem(b"\r\nRING\r\nCONNECT 9600/RLP\r\n")
    call = modem.answer(IncomingCall())
    assert call.connect == "CONNECT 9600/RLP"
    assert modem.port.written == b"ATA\r"


def test_answer_raises_when_the_caller_gives_up():
    modem = make_modem(b"\r\nNO CARRIER\r\n")
    with pytest.raises(ModemError, match="NO CARRIER"):
        modem.answer(IncomingCall())


def test_answer_reports_a_call_that_came_up_without_a_carrier():
    modem = make_modem(b"\r\nOK\r\n")
    with pytest.raises(NoDataCarrier):
        modem.answer(IncomingCall())


def test_voice_calls_are_not_data_calls():
    assert not IncomingCall(bearer="VOICE").is_data
    assert not IncomingCall(bearer="FAX").is_data
    assert IncomingCall(bearer="REL ASYNC").is_data
    assert IncomingCall(bearer=None).is_data


def test_init_sequence_pins_the_bit_rate_to_the_serial_speed():
    modem = Modem(SerialConfig(baudrate=19200), ModemConfig(fix_bit_rate=True))
    assert "AT+IPR=19200" in modem.init_sequence()
    modem = Modem(SerialConfig(), ModemConfig(fix_bit_rate=False))
    assert not [c for c in modem.init_sequence() if c.startswith("AT+IPR")]


def test_init_sequence_reflects_the_configuration():
    modem = make_modem(bearer="71,0,1", single_numbering=4, init_commands=["AT\\Q3"])
    sequence = modem.init_sequence()
    assert "AT+CBST=71,0,1" in sequence
    assert "AT+CSNS=4" in sequence
    assert "AT+CLIP=1" in sequence
    assert sequence[-1] == "AT\\Q3"
    assert "ATS0=0" in sequence  # we answer with ATA, never automatically


def test_init_sequence_omits_disabled_options():
    modem = make_modem(bearer=None, single_numbering=-1, caller_id=False, extended_ring=False)
    sequence = modem.init_sequence()
    assert not [command for command in sequence if command.startswith(("AT+CBST", "AT+CSNS",
                                                                      "AT+CLIP", "AT+CRC"))]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('+CLIP: "+390123456789",145,,,,0', "+390123456789"),
        ('+CLIP: "",128,,,,1', None),
        ("+CLIP: 0123456789,129", "0123456789"),
    ],
)
def test_parse_clip(line, expected):
    assert _parse_clip(line) == expected


@pytest.mark.parametrize(
    ("bearer", "expected"),
    [
        ("7,0,1", "9600 bps V.32, non-transparent"),
        ("71,0,0", "9600 bps V.110, transparent"),
        ("99,0,1", "speed code 99, non-transparent"),
        (None, "modem default"),
    ],
)
def test_describe_bearer(bearer, expected):
    assert describe_bearer(bearer) == expected
