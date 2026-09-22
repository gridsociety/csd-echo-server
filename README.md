# csd-echo-server

Answers GSM circuit-switched data (CSD) calls on a serial modem, greets the
caller with a configurable welcome message and a summary of the connection, and
then echoes back everything it receives until the caller hangs up.

It is the `echo` service of the dial-up world: a fixed endpoint you can call
from another modem to check that a CSD bearer really works end to end, that your
terminal settings are right, and that bytes survive the round trip.

```
This is a CSD echo server.

Call           : #1 at 2026-03-14 09:21:44 UTC
Caller         : +390123456789
Call type      : REL ASYNC
Connection     : CONNECT 9600/RLP
Bearer service : 9600 bps V.32, non-transparent
Local link     : 115200 8N1
Idle timeout   : 300 s
Call limit     : 3600 s

Everything you send is echoed back. Hang up to end the call.
```

## Requirements

- Python 3.11 or newer.
- A modem that speaks AT commands over a serial port and supports CSD
  (`AT+CBST=?` returns a list of bearer services). Plain GSM/GPRS modules
  usually do; most LTE-only modules do not.
- A SIM whose subscription has data calls enabled, on a network that still
  carries them. Both are worth checking before blaming the software.
- Read and write access to the serial device, which on most distributions means
  membership of the `dialout` group.

## Install

With [uv](https://docs.astral.sh/uv/), straight from a checkout:

```sh
git clone https://github.com/gridsociety/csd-echo-server
cd csd-echo-server
uv run csd-echo-server --port /dev/ttyUSB0
```

`uv run` creates the virtual environment and installs the dependencies on first
use, so there is nothing else to set up.

To install it as a command of its own, for the current user:

```sh
uv tool install git+https://github.com/gridsociety/csd-echo-server
```

Or with pip, into a virtual environment of your choice:

```sh
pip install git+https://github.com/gridsociety/csd-echo-server
```

## Use

Check that the modem is reachable and the SIM is ready before serving anything:

```sh
csd-echo-server --port /dev/ttyUSB0 --check
```

```
port            : /dev/ttyUSB0 at 115200 baud
modem           : Cinterion MC55i REVISION 02.001
SIM             : +CPIN: READY
registration    : +CREG: 0,1
signal          : +CSQ: 14,99
bearer          : +CBST: 7,0,1
```

Then run the server:

```sh
csd-echo-server --port /dev/ttyUSB0
```

It opens the port, puts the modem into a known state, and waits. Every incoming
data call is answered with `ATA`, gets the welcome message, and is echoed back
to until the caller hangs up or a timeout expires. The full option list is in
`--help`; the interesting ones are `--config`, `--port`, `--baud`, `--welcome`
and `--log-level DEBUG`, which logs every AT command exchanged with the modem.

## Configuration

Everything beyond those few flags lives in a TOML file, passed with `--config`
and read from `/etc/csd-echo-server.toml` when it exists.
[`config.example.toml`](config.example.toml) documents every key; the defaults
are sensible for a GSM modem on a SIM without a separate data number, so a
working file can be as short as:

```toml
[serial]
port = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0"

[echo]
welcome = "This is a CSD echo server. Everything you type comes back."
```

Two settings deserve a word:

- **`modem.bearer`** is the argument of `AT+CBST`, the CSD bearer service to
  request. The default `"7,0,1"` asks for 9600 bps V.32, asynchronous,
  non-transparent (RLP), which is the combination most networks still carry.
  `AT+CBST=?` lists what your modem supports; the network and the calling side
  have to agree with it.
- **`modem.single_numbering`** is the argument of `AT+CSNS`. A SIM with a single
  number signals incoming calls without a bearer element, and the modem would
  treat them as voice calls and never hand over a data stream. The default of
  `4` tells the modem to read those calls as data, which is what makes such a
  SIM usable at all. If your SIM has a separate data number, the network signals
  the bearer itself and you can set this to `-1` to leave it alone.
- **`call.reject_voice_calls`** is off by default, so every incoming call is
  answered, whatever the network signalled — which is how industrial CSD modems
  behave, and the only thing that works on a SIM that signals data calls as
  voice. A call that really is a voice call comes up without a data carrier; the
  server notices, hangs up and goes back to waiting. Turn the flag on if the
  number also receives voice calls you would rather not pick up.

A `/dev/serial/by-id/...` path is worth preferring over `/dev/ttyUSB0`: it
survives reboots and re-plugging, where the numbered device may not.

## Running as a systemd service

The unit in [`packaging/`](packaging/csd-echo-server.service) runs the server
from a self-contained virtual environment in `/opt/csd-echo-server`, as an
unprivileged dynamic user that is a member of `dialout` and has access to
nothing but the serial port.

Build that environment with uv (`pip` works the same way if you prefer; the
service itself does not need uv at runtime):

```sh
git clone https://github.com/gridsociety/csd-echo-server /tmp/csd-echo-server
cd /tmp/csd-echo-server
sudo uv venv /opt/csd-echo-server
sudo uv pip install --python /opt/csd-echo-server/bin/python .
```

Install the configuration and the unit:

```sh
sudo cp config.example.toml /etc/csd-echo-server.toml
sudo $EDITOR /etc/csd-echo-server.toml          # at least set serial.port
sudo cp packaging/csd-echo-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now csd-echo-server
```

Then watch it work:

```sh
systemctl status csd-echo-server
journalctl -u csd-echo-server -f
```

The unit expects the serial device to be a `ttyUSB`, `ttyACM` or `ttyS` device
and its group to be `dialout`. On a distribution that uses `uucp` for serial
ports, change `SupplementaryGroups=` accordingly. To upgrade, pull the new
version and repeat the `uv pip install` step, then
`sudo systemctl restart csd-echo-server`.

## Calling in

Any modem or terminal program that can place a data call will do. From another
AT-capable modem, ask for the same bearer service and dial:

```
AT+CBST=7,0,1
ATD<number>
```

A `CONNECT` line means the bearer is up; the welcome message follows within a
second or so. Typed characters come back as they are echoed, so a plain terminal
emulator is enough to see it working. `ATH`, or dropping DTR, ends the call.

## How it handles a call

1. The init sequence puts the modem in a known state: `ATE0` for parseable
   replies, `AT&C1` so DCD follows the carrier, `AT&D2` so dropping DTR hangs
   up, `ATS0=0` so the modem never answers on its own, plus `AT+CRC=1` and
   `AT+CLIP=1` for the extended ring report and the caller number.
2. On `RING` or `+CRING:` the server collects the call details for a moment and
   answers with `ATA` — unless `call.reject_voice_calls` is on and the network
   marked the call as voice or fax. A call answered without a data carrier is
   hung up again.
3. After `CONNECT` it sends the banner and echoes every byte it receives. Line
   endings are normalised to CRLF unless `echo.newline = "raw"`.
4. The call ends when the carrier drops — detected on DCD where the cable
   carries it, and from the modem's own `NO CARRIER` report otherwise — or when
   the idle timeout or the call limit expires.
5. The modem is returned to command mode by dropping DTR, and the server goes
   back to waiting. A modem that stops responding is closed, reopened and
   re-initialised after `modem.retry_delay` seconds.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```

The tests drive the AT parser, the banner and the echo loop against a fake
serial port, so they run anywhere, with no modem attached.

## License

MIT. See [LICENSE](LICENSE).
