from __future__ import annotations

from core.config import (
    DEFAULT_FALLBACK_UPSTREAMS,
    DaemonSettings,
    UserConfig,
    settings_file,
)
from core.jsonio import write_json_atomic
from core.paths import config_file


def test_user_config_defaults_when_absent():
    config = UserConfig.load()
    assert config.language == ""
    assert config.theme == "dark"


def test_user_config_round_trip():
    UserConfig(language="ru", theme="dark", last_tab=2).save()
    assert UserConfig.load().language == "ru"
    assert UserConfig.load().last_tab == 2


def test_user_config_survives_a_garbage_file():
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    assert UserConfig.load().theme == "dark"


def test_user_config_ignores_wrongly_typed_fields():
    write_json_atomic(config_file(), {"language": 7, "last_tab": "x"}, harden=False)
    config = UserConfig.load()
    assert config.language == ""
    assert config.last_tab == 0


def test_daemon_settings_defaults():
    settings = DaemonSettings.load()
    assert settings.listen_address == "127.0.0.1"
    assert settings.listen_port == 53
    assert settings.upstreams == []
    assert settings.fallback_upstreams == list(DEFAULT_FALLBACK_UPSTREAMS)


def test_daemon_settings_round_trip():
    DaemonSettings(listen_address="127.0.0.2", upstreams=["192.168.0.1"]).save()
    loaded = DaemonSettings.load()
    assert loaded.listen_address == "127.0.0.2"
    assert loaded.upstreams == ["192.168.0.1"]


def test_a_zero_poll_interval_cannot_produce_a_busy_loop():
    DaemonSettings(traefik_poll_seconds=0).save()
    assert DaemonSettings.load().traefik_poll_seconds >= 1


def test_an_empty_fallback_list_falls_back_to_the_defaults():
    write_json_atomic(
        settings_file(),
        {"fallback_upstreams": []},
        harden=False,
    )
    assert DaemonSettings.load().fallback_upstreams == list(DEFAULT_FALLBACK_UPSTREAMS)


def test_non_string_entries_are_dropped_from_list_fields():
    write_json_atomic(
        settings_file(),
        {"upstreams": ["1.1.1.1", 5, None, ""]},
        harden=False,
    )
    assert DaemonSettings.load().upstreams == ["1.1.1.1"]


def test_saving_settings_never_shells_out_on_windows(monkeypatch):
    """Regression: DaemonSettings.save() spawned icacls on Windows and broke CI."""
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    # The autouse no_subprocess fixture fails the test if a process is started.
    DaemonSettings(listen_address="127.0.0.3").save()
    assert DaemonSettings.load().listen_address == "127.0.0.3"


def test_saving_user_config_never_shells_out_on_windows(monkeypatch):
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    UserConfig(language="ru").save()
    assert UserConfig.load().language == "ru"
