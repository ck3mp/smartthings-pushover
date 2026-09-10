import logging
from types import MappingProxyType, SimpleNamespace

from smartthings_pushover import cli
from smartthings_pushover.config import ApplianceConfig
from smartthings_pushover.health import Heartbeat


def _app(names):
    return ApplianceConfig(kind="washer", ip="1.2.3.4", port=None, name="W",
                           course_names=MappingProxyType(names))


def test_print_courses_lists_unnamed(capsys):
    state = SimpleNamespace(course="1C")
    cli._print_courses(_app({"1C": "Eco", "ZZ": "Ghost"}), state, ["1C", "1B", "25"])
    out = capsys.readouterr().out
    assert "Current course code: 1C -> Eco" in out
    assert " * 1C  Eco" in out and "   1B  (unnamed)" in out
    assert "2 unnamed" in out
    assert 'WASHER_COURSE_NAMES=1C=Eco,1B=...,25=...' in out
    assert "does not advertise: ZZ" in out


def test_print_courses_all_named(capsys):
    cli._print_courses(_app({"1C": "Eco"}), SimpleNamespace(course="1C"), ["1C"])
    assert "All courses named." in capsys.readouterr().out


def test_print_courses_without_table(capsys):
    cli._print_courses(_app({}), SimpleNamespace(course="1C"), [])
    out = capsys.readouterr().out
    assert 'add to WASHER_COURSE_NAMES, e.g. WASHER_COURSE_NAMES="1C=Cotton"' in out


def test_healthcheck_uses_heartbeat_env(tmp_path, monkeypatch):
    path = tmp_path / "beat"
    monkeypatch.setenv("HEARTBEAT_PATH", str(path))
    assert cli.main(["--healthcheck"]) == 1
    Heartbeat(path).touch()
    assert cli.main(["--healthcheck"]) == 0


def test_load_env_file_docker_semantics(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "\n"
        "WASHER_NAME=Washing machine\n"
        "WASHER_COURSE_NAMES=1C=Eco 40-60,A0=Quick Wash 15'\n"
        "QUOTED=\"keep quotes\"\n"
        "EXISTING=from-file\n"
        "  SPACED = padded \n"
        "garbage line without equals\n"
    )
    env = {"EXISTING": "from-env"}
    assert cli.load_env_file(str(f), env) == 4
    assert env == {
        "EXISTING": "from-env",
        "WASHER_NAME": "Washing machine",
        "WASHER_COURSE_NAMES": "1C=Eco 40-60,A0=Quick Wash 15'",
        "QUOTED": '"keep quotes"',
        "SPACED": "padded",
    }


def test_env_file_flag(tmp_path, monkeypatch, capsys):
    # delenv registers these keys for restoration, so whatever main() loads
    # into os.environ from the file is undone after the test.
    for k in ("WASHER_IP", "DRYER_IP", "WASHER_ENABLED", "DRYER_ENABLED",
              "PUSHOVER_TOKEN", "PUSHOVER_USER"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("WASHER_IP=192.0.2.9\nPUSHOVER_USER=u\n")
    assert cli.main(["--env-file", str(f)]) == 1
    assert "PUSHOVER_TOKEN" in capsys.readouterr().err  # file was read: only the token is missing
    assert cli.main(["--env-file", str(tmp_path / "missing")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_config_error_exit_code(monkeypatch, capsys):
    for k in ("WASHER_IP", "DRYER_IP", "WASHER_ENABLED", "DRYER_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main([]) == 1
    assert "config error" in capsys.readouterr().err


def test_library_logger_never_louder_than_root():
    cli._setup_logging("WARNING")
    assert logging.getLogger("smartthings_local").getEffectiveLevel() == logging.WARNING
    cli._setup_logging("DEBUG")
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("smartthings_local").setLevel(max(logging.INFO, logging.DEBUG))
    assert logging.getLogger("smartthings_local").getEffectiveLevel() == logging.INFO
