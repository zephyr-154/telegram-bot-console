from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import psutil
import pytest

from app import manager as manager_module
from app.config import ConfigurationError, read_dotenv
from app.manager import BotManager, ROOT_MARKER
from app.settings_editor import initial_values, save_config
from app.storage import read_json, write_json


@pytest.fixture
def manager(tmp_path):
    return BotManager(tmp_path / "managed")


def configure(manager, name, bot_id="9988776655"):
    folder = manager.create(name)
    font = folder / "test-font.ttf"
    font.touch()
    values = initial_values(folder / ".env")
    values.update(BOT_TOKEN=f"{bot_id}:offline_test", CHANNEL_ID="-100987654", ADMIN_USER_ID="42",
                  FONT_REGULAR_PATH=str(font), FONT_BOLD_PATH=str(font))
    save_config(folder / ".env", values, (folder / ".env").read_bytes())
    return folder


def test_profiles_are_private_independent_and_case_insensitive(manager):
    first, second = manager.create("校園一號"), manager.create("School-2")
    assert manager.names() == ["School-2", "校園一號"]
    assert manager.folder("school-2") == second
    assert read_dotenv(first / ".env")["CONTENT_HASH_SECRET"] != read_dotenv(second / ".env")["CONTENT_HASH_SECRET"]
    assert manager.status("School-2")["status"] == "unconfigured"
    assert (manager.root / ROOT_MARKER).is_file()
    assert (manager.root / ".gitignore").read_text() == "*\n"
    with pytest.raises(ConfigurationError, match="已經存在"):
        manager.create("sCHOOL-2")
    if os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o700
        assert (first / ".env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["../escape", "", "-hidden", "a/b", "a\\b", "con", "LPT1", "abc\n", "x" * 41])
def test_invalid_profile_names_cannot_escape_managed_folder(manager, name):
    with pytest.raises(ConfigurationError):
        manager.create(name)
    assert not manager.names()


def test_duplicate_telegram_identity_and_external_database_are_rejected(manager):
    configure(manager, "first")
    folder = configure(manager, "second")
    values = read_dotenv(folder / ".env")
    with pytest.raises(ConfigurationError, match="重複部署"):
        manager.validate_values("second", values)
    values["BOT_TOKEN"] = "8877665544:other_offline_token"
    values["DATABASE_PATH"] = str(manager.root.parent / "unrelated.sqlite3")
    with pytest.raises(ConfigurationError, match="專用目錄"):
        manager.validate_values("second", values)


def test_delete_only_removes_selected_managed_profile(manager, tmp_path):
    first = manager.create("first")
    second = manager.create("second")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    manager.delete("first")
    assert not first.exists() and second.exists() and unrelated.read_text() == "keep"
    assert manager.names() == ["second"]


def test_manager_rejects_unowned_or_dangerous_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError):
        BotManager(tmp_path).ensure_root()
    manager = BotManager(tmp_path / "bad")
    manager.root.mkdir()
    write_json(manager.root / ROOT_MARKER, {"application": "different-application"})
    with pytest.raises(ConfigurationError):
        manager.ensure_root()


FAKE_WORKER = r'''
import argparse, json, os, time
from pathlib import Path
import psutil
parser = argparse.ArgumentParser()
for key in ("root", "name", "nonce"):
    parser.add_argument("--" + key, required=True)
args = parser.parse_args()
folder = Path(args.root) / "bots" / args.name
state_path = folder / "runtime.json"
deadline = time.monotonic() + 10
while True:
    try:
        state = json.loads(state_path.read_text())
        if state.get("nonce") == args.nonce:
            break
    except (FileNotFoundError, ValueError):
        pass
    if time.monotonic() > deadline:
        raise SystemExit(8)
    time.sleep(.03)
state.update(pid=os.getpid(), create_time=psutil.Process().create_time(), status="running")
temporary = folder / "fake.tmp"
temporary.write_text(json.dumps(state))
os.replace(temporary, state_path)
if os.environ.get("FAKE_WORKER_FAIL"):
    raise SystemExit(9)
while True:
    try:
        if (folder / "stop.request").read_text() == args.nonce:
            break
    except FileNotFoundError:
        pass
    time.sleep(.03)
'''


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    source = tmp_path / "fake-source"
    (source / "app").mkdir(parents=True)
    (source / "tgbot.cmd").touch()
    (source / "app" / "__init__.py").touch()
    (source / "app" / "worker.py").write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(manager_module, "__file__", str(source / "app" / "manager.py"))


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail("Background worker did not reach the expected state")


def test_real_background_workers_start_independently_and_stop_gracefully(manager, fake_worker):
    configure(manager, "first")
    configure(manager, "second", "8877665544")
    try:
        assert "背景" in manager.start("first")
        assert "背景" in manager.start("second")
        wait_for(lambda: manager.status("first")["status"] == "running")
        wait_for(lambda: manager.status("second")["status"] == "running")
        first_pid = manager.status("first")["pid"]
        assert first_pid != manager.status("second")["pid"]
        assert manager.status("first")["memory_mb"] > 0
        assert manager.start("first") == "已在執行"
        assert manager.status("first")["pid"] == first_pid
        manager.stop("first")
        assert manager.process("first") is None
        assert manager.process("second") is not None
        assert manager.all("stop")
        assert manager.process("second") is None
    finally:
        manager.all("stop")


def test_stale_pid_record_cannot_terminate_an_unrelated_process(manager):
    folder = manager.create("first")
    process = psutil.Process()
    write_json(folder / "runtime.json", {"pid": process.pid, "create_time": process.create_time(), "nonce": "foreign", "status": "running"})
    assert manager.process("first") is None
    assert manager.stop("first") == "已停止"
    assert psutil.pid_exists(os.getpid())
    assert manager.status("first")["status"] == "failed"


def test_worker_crash_is_shown_as_failed_instead_of_running(manager, fake_worker, monkeypatch):
    folder = configure(manager, "first")
    monkeypatch.setenv("FAKE_WORKER_FAIL", "1")
    manager.start("first")
    wait_for(lambda: manager.status("first")["status"] == "failed")
    assert manager.process("first") is None
    assert (folder / ".env").is_file()


def test_worker_stop_request_cancels_application_and_records_state(manager, monkeypatch):
    import app.main
    from app.worker import serve
    folder = configure(manager, "first")
    nonce = "a" * 32
    write_json(folder / "runtime.json", {"nonce": nonce, "status": "starting"})
    ended = []
    async def fake_run(env_path, *, ready_callback, use_environment):
        assert env_path == folder / ".env" and use_environment is False
        ready_callback()
        try:
            await asyncio.sleep(100)
        finally:
            ended.append(True)
    monkeypatch.setattr(app.main, "run", fake_run)
    async def scenario():
        task = asyncio.create_task(serve(manager, "first", nonce))
        while read_json(folder / "runtime.json").get("status") != "running":
            await asyncio.sleep(.01)
        (folder / "stop.request").write_text(nonce)
        await asyncio.wait_for(task, timeout=4)
    asyncio.run(scenario())
    assert ended and read_json(folder / "runtime.json")["status"] == "stopped"


def test_profile_runtime_does_not_inherit_global_credentials(manager, monkeypatch):
    from app.config import load_settings
    folder = configure(manager, "first")
    monkeypatch.setenv("BOT_TOKEN", "global-secret-must-not-be-used")
    monkeypatch.setenv("CHANNEL_ID", "-100111")
    settings = load_settings(folder / ".env", use_environment=False)
    assert settings.bot_token == "9988776655:offline_test" and settings.channel_id == -100987654


def test_uninstall_blocks_new_workers_and_edits_but_allows_stopping(manager):
    folder = configure(manager, "first")
    (manager.root / ".uninstalling").touch()
    with pytest.raises(ConfigurationError, match="解除安裝"):
        manager.start("first")
    with pytest.raises(ConfigurationError, match="解除安裝"):
        manager.create("second")
    with pytest.raises(ConfigurationError, match="解除安裝"):
        manager.save("first", read_dotenv(folder / ".env"), (folder / ".env").read_bytes())
    assert manager.stop("first") == "已停止"


def test_worker_logs_redact_credentials_and_exception_content():
    import logging
    from app.worker import PrivateLogFilter
    secret = "9988776655:private_token"
    handler_filter = PrivateLogFilter([secret])
    record = logging.LogRecord("test", logging.WARNING, "", 0, "request %s", (secret,), None)
    assert handler_filter.filter(record) and secret not in record.getMessage()
    try:
        raise ValueError("private-submission-text")
    except ValueError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, "", 0, "failure private-submission-text", (), sys.exc_info())
        handler_filter.filter(record)
    assert "private-submission-text" not in record.getMessage() and record.exc_info is None


def test_still_alive_worker_shows_stopping_until_process_exits(manager, monkeypatch):
    folder = manager.create("first")
    write_json(folder / "runtime.json", {"status": "stopped"})
    monkeypatch.setattr(manager, "process", lambda name: psutil.Process())
    assert manager.status("first")["status"] == "stopping"
