import pytest

from csd_echo_server.config import Config, ConfigError


def test_defaults_are_valid():
    config = Config()
    config.validate()
    assert config.serial.baudrate == 115200
    assert config.echo.welcome.startswith("This is a CSD echo server")


def test_from_mapping_overrides_only_what_is_given():
    config = Config.from_mapping({"serial": {"port": "/dev/ttyACM0"}, "echo": {"welcome": "hi"}})
    assert config.serial.port == "/dev/ttyACM0"
    assert config.serial.baudrate == 115200
    assert config.echo.welcome == "hi"
    assert config.echo.show_call_info is True


def test_voice_calls_are_answered_by_default():
    assert Config().call.voice_calls == "accept"


@pytest.mark.parametrize("action", ["accept", "reject", "ignore"])
def test_every_voice_call_action_is_accepted(action):
    assert Config.from_mapping({"call": {"voice_calls": action}}).call.voice_calls == action


def test_unknown_voice_call_action_is_rejected():
    with pytest.raises(ConfigError, match=r"call\.voice_calls"):
        Config.from_mapping({"call": {"voice_calls": "hang up"}})


def test_unknown_section_is_rejected():
    with pytest.raises(ConfigError, match="unknown section"):
        Config.from_mapping({"nope": {}})


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_mapping({"serial": {"prot": "/dev/ttyUSB0"}})


def test_bad_newline_mode_is_rejected():
    with pytest.raises(ConfigError, match=r"echo\.newline"):
        Config.from_mapping({"echo": {"newline": "lf"}})


def test_bad_single_numbering_is_rejected():
    with pytest.raises(ConfigError, match="single_numbering"):
        Config.from_mapping({"modem": {"single_numbering": 3}})


def test_load_from_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[serial]\nport = "/dev/ttyS0"\n\n[call]\nidle_timeout = 30\n')
    config = Config.load(path)
    assert config.serial.port == "/dev/ttyS0"
    assert config.call.idle_timeout == 30


def test_load_reports_broken_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[serial\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        Config.load(path)


def test_load_without_path_returns_defaults():
    assert Config.load(None).serial.port == Config().serial.port


def test_example_config_matches_the_schema():
    Config.load("config.example.toml")
