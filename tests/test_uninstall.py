from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.manager import BotManager, ROOT_MARKER
from app.storage import read_json, write_json
from app.uninstall import remove_installation, validate_removal_root


def plan_for(manager, command):
    return {"root": str(manager.root), "identity": read_json(manager.root / ROOT_MARKER)["id"],
            "parent_pid": 0, "command": command}


def test_uninstall_removes_only_marked_data_after_successful_package_removal(tmp_path):
    manager = BotManager(tmp_path / "managed")
    manager.create("first")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep")
    remove_installation(plan_for(manager, [sys.executable, "-c", "raise SystemExit(0)"]))
    assert not manager.root.exists()
    assert unrelated.read_text() == "keep"


def test_failed_package_uninstall_preserves_all_private_data(tmp_path):
    manager = BotManager(tmp_path / "managed")
    manager.create("first")
    (manager.root / ".uninstalling").touch()
    with pytest.raises(RuntimeError, match="資料已保留"):
        remove_installation(plan_for(manager, [sys.executable, "-c", "raise SystemExit(4)"]))
    assert (manager.folder("first") / ".env").is_file()
    assert not (manager.root / ".uninstalling").exists()


def test_changed_or_missing_ownership_marker_refuses_recursive_deletion(tmp_path):
    manager = BotManager(tmp_path / "managed")
    manager.create("first")
    plan = plan_for(manager, [sys.executable, "-c", "raise SystemExit(0)"])
    write_json(manager.root / ROOT_MARKER, {"application": "other", "id": plan["identity"]})
    with pytest.raises(ValueError):
        remove_installation(plan)
    assert manager.root.exists()


def test_uninstall_command_requires_confirmation_and_does_not_execute_on_decline(tmp_path, monkeypatch):
    from app import cli, uninstall
    monkeypatch.setenv("TGBOT_HOME", str(tmp_path / "managed"))
    called = []
    monkeypatch.setattr(uninstall, "schedule_uninstall", lambda manager: called.append(True))
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert cli.main(["app", "delete"]) == 0
    assert not called


def test_explicit_uninstall_confirmation_schedules_helper(tmp_path, monkeypatch):
    from app import cli, uninstall
    monkeypatch.setenv("TGBOT_HOME", str(tmp_path / "managed"))
    called = []
    def schedule(manager):
        called.append(manager.root)
        return tmp_path / "result.log"
    monkeypatch.setattr(uninstall, "schedule_uninstall", schedule)
    assert cli.main(["app", "delete", "--yes"]) == 0
    assert called == [tmp_path / "managed"]


def test_failed_helper_staging_releases_uninstall_guard(tmp_path, monkeypatch):
    from app import uninstall
    manager = BotManager(tmp_path / "managed")
    manager.ensure_root()
    prefix = tmp_path / "tool"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text('[tool]\nrequirements = [{name = "telegram-anonymous-board"}]\n')
    monkeypatch.setattr(uninstall.sys, "prefix", str(prefix))
    monkeypatch.setattr(uninstall.shutil, "which", lambda command: "uv")
    def fail_staging(**kwargs):
        raise PermissionError("test")
    monkeypatch.setattr(uninstall.tempfile, "mkdtemp", fail_staging)
    with pytest.raises(PermissionError):
        uninstall.schedule_uninstall(manager)
    assert not (manager.root / ".uninstalling").exists()
    manager.ensure_available()
