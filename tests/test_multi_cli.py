from __future__ import annotations

import pytest

from app import cli, terminal
from app.manager import BotManager


@pytest.fixture
def configured_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TGBOT_HOME", str(tmp_path / "managed"))
    manager = BotManager()
    manager.create("schoolA")
    manager.create("schoolB")
    return manager


def test_cli_add_and_names_do_not_expose_tokens(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TGBOT_HOME", str(tmp_path / "managed"))
    assert cli.main(["add", "school", "--no-config"]) == 0
    assert BotManager().names() == ["school"]
    assert cli.main(["names"]) == 0
    assert "school" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["start", "stop"])
def test_no_name_operates_all_and_name_only_operates_selected(configured_manager, monkeypatch, command):
    calls = []
    monkeypatch.setattr(BotManager, command, lambda self, name, **kwargs: calls.append(name) or "done")
    assert cli.main([command]) == 0
    assert calls == ["schoolA", "schoolB"]
    calls.clear()
    assert cli.main([command, "schoolB"]) == 0
    assert calls == ["schoolB"]


def test_restart_restarts_all_registered_bots(configured_manager, monkeypatch):
    calls = []
    monkeypatch.setattr(BotManager, "stop", lambda self, name, **kwargs: calls.append(("stop", name)))
    monkeypatch.setattr(BotManager, "start", lambda self, name: calls.append(("start", name)) or "started")
    assert cli.main(["restart"]) == 0
    assert calls == [("stop", "schoolA"), ("start", "schoolA"), ("stop", "schoolB"), ("start", "schoolB")]


def test_select_flag_uses_name_menu_instead_of_starting_every_bot(configured_manager, monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, "select", lambda title, options: "schoolB")
    monkeypatch.setattr(BotManager, "start", lambda self, name: calls.append(name) or "started")
    assert cli.main(["start", "--select"]) == 0
    assert calls == ["schoolB"]


def test_partial_name_filters_the_name_picker(configured_manager, monkeypatch):
    shown = []
    monkeypatch.setattr(terminal, "select", lambda title, options: shown.extend(options) or "schoolB")
    monkeypatch.setattr(terminal, "settings_panel", lambda manager, name: None)
    assert cli.main(["config", "school"]) == 0
    assert shown == [("schoolA", "schoolA"), ("schoolB", "schoolB")]


def test_delete_one_bot_requires_confirmation_and_preserves_other_bot(configured_manager, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert cli.main(["delete", "schoolA"]) == 0
    assert configured_manager.names() == ["schoolA", "schoolB"]
    assert cli.main(["delete", "schoolA", "--yes"]) == 0
    assert configured_manager.names() == ["schoolB"]


def test_unknown_name_lists_known_names_and_returns_failure(configured_manager, capsys):
    assert cli.main(["start", "missing"]) == 1
    assert "schoolA" in capsys.readouterr().err


def test_bad_configuration_in_one_bot_does_not_skip_remaining_bots(configured_manager, monkeypatch, capsys):
    from app.config import ConfigurationError
    calls = []
    def start(self, name):
        calls.append(name)
        if name == "schoolA":
            raise ConfigurationError("缺少設定")
        return "started"
    monkeypatch.setattr(BotManager, "start", start)
    assert cli.main(["start"]) == 1
    assert calls == ["schoolA", "schoolB"]


def test_status_reports_drafts_without_claiming_they_are_running(configured_manager, capsys):
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "待設定" in output and "schoolA" in output and "schoolB" in output


def test_managed_backups_ignore_external_database_environment(configured_manager, monkeypatch):
    calls = []
    import app.backup
    monkeypatch.setenv("DATABASE_PATH", "unrelated.sqlite3")
    monkeypatch.setattr(app.backup, "main", lambda args, **kwargs: calls.append((args, kwargs)))
    assert cli.main(["backup", "schoolA"]) == 0
    assert calls == [(["create"], {"env_path": configured_manager.folder("schoolA") / ".env", "use_environment": False})]
