from __future__ import annotations

import io
import asyncio

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from app.manager import BotManager
from app.settings_editor import initial_values, render_env, save_config
from app.config import parse_ids, read_dotenv
from app.terminal import select, settings_panel, text_row
from app.chat_discovery import ChatRegistry
from aiogram.types import Chat


@pytest.fixture
def profile(tmp_path):
    manager = BotManager(tmp_path / "managed")
    folder = manager.create("school")
    font = folder / "font.ttf"
    font.touch()
    values = initial_values(folder / ".env")
    values.update(BOT_TOKEN="9988776655:private_offline_token", CHANNEL_ID="-100123", ADMIN_USER_ID="42",
                  FONT_REGULAR_PATH=str(font), FONT_BOLD_PATH=str(font))
    save_config(folder / ".env", values, (folder / ".env").read_bytes())
    return manager, folder, values


def test_arrow_key_then_enter_selects_current_row():
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        assert select("選擇", [("first", "第一台"), ("second", "第二台")], input=pipe, output=DummyOutput()) == "second"


def test_escape_leaves_selector_without_taking_action():
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b")
        assert select("選擇", [("first", "第一台")], input=pipe, output=DummyOutput()) is None


def test_settings_enter_edits_selected_value_and_ctrl_s_saves(profile):
    manager, folder, values = profile
    original = (folder / ".env").read_bytes()
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 3 + "\r\x01\x0b84\r\x13\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    saved = read_dotenv(folder / ".env")
    assert saved["ADMIN_USER_ID"] == "84"
    assert saved["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"]
    assert (folder / ".env.bak").read_bytes() == original


def test_settings_escape_discards_unsaved_changes(profile):
    manager, folder, _ = profile
    original = (folder / ".env").read_bytes()
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 3 + "\r\x01\x0b84\r\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    assert (folder / ".env").read_bytes() == original


def test_settings_never_renders_existing_token(profile):
    manager, _, values = profile
    screen = io.StringIO()
    output = Vt100_Output(screen, lambda: Size(rows=32, columns=110), term="xterm-256color", enable_cpr=False)
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b")
        settings_panel(manager, "school", input=pipe, output=output)
    assert values["BOT_TOKEN"] not in screen.getvalue()
    assert "已設定" in screen.getvalue()


def test_profile_actions_are_reachable_with_arrow_keys(profile):
    manager, _, _ = profile
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 9 + "\r")
        assert settings_panel(manager, "school", input=pipe, output=DummyOutput()) == "start"


def test_chinese_labels_align_using_display_width():
    assert text_row("管理員", "42", 12) == "管理員" + " " * 6 + "42"


@pytest.mark.parametrize("keys,expected", [
    ("\x1b[B\r", "-1009"),
    ("\x01\x0b-100777\r", "-100777"),
    ("\x01\x0b社群\x1b[B\r", "-1008"),
    ("\x01\x0b1008\x1b[B\r", "-1008"),
    ("\x1b[B\x13", "-1009"),
])
def test_target_can_be_selected_searched_or_entered_manually(profile, keys, expected):
    manager, folder, values = profile
    registry = ChatRegistry(folder, values["BOT_TOKEN"])
    registry.remember(Chat(id=-1009, title="Campus 頻道", type="channel"))
    registry.remember(Chat(id=-1008, title="學校社群", type="supergroup"))
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 2 + "\r" + keys + "\x13\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    assert read_dotenv(folder / ".env")["CHANNEL_ID"] == expected


def test_discovery_can_start_with_only_unsaved_token_and_closes_on_escape(tmp_path, monkeypatch):
    import app.terminal as terminal
    manager = BotManager(tmp_path / "managed")
    folder = manager.create("new")
    original = (folder / ".env").read_bytes()
    calls, closed = [], []
    with create_pipe_input() as pipe:
        async def watch(manager_arg, name, token, channel_id):
            calls.append((name, token, channel_id))
            try:
                yield [], "等待邀請機器人"
                pipe.send_text("\x1b")
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        monkeypatch.setattr(terminal, "watch_chats", watch)
        pipe.send_text("\r9988776655:offline_token\r\x1b[B\r")
        settings_panel(manager, "new", input=pipe, output=DummyOutput())
    assert calls == [("new", "9988776655:offline_token", "")]
    assert closed == [True]
    assert (folder / ".env").read_bytes() == original


def test_unsaved_token_and_live_result_can_be_selected_then_saved(profile, monkeypatch):
    import app.terminal as terminal
    manager, folder, values = profile
    new_token = "8877665544:unsaved_offline_token"
    closed = []
    with create_pipe_input() as pipe:
        async def watch(manager_arg, name, token, channel_id):
            assert token == new_token
            assert read_dotenv(folder / ".env")["BOT_TOKEN"] == values["BOT_TOKEN"]
            try:
                yield [{"id": -10099, "title": "即時新群組", "type": "supergroup"}], "等待邀請"
                pipe.send_text("\x1b[B\r\x1b[B\r\x13\x1b")
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        monkeypatch.setattr(terminal, "watch_chats", watch)
        pipe.send_text("\r" + new_token + "\r\x1b[B\r")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    saved = read_dotenv(folder / ".env")
    assert saved["BOT_TOKEN"] == new_token and saved["CHANNEL_ID"] == "-10099"
    assert saved["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"] and closed == [True]


def test_discovery_error_keeps_manual_target_input_available(profile, monkeypatch):
    import app.terminal as terminal
    from app.config import ConfigurationError
    manager, folder, _ = profile
    with create_pipe_input() as pipe:
        async def watch(*args):
            pipe.send_text("\x1b[B\r\x01\x0b-10055\r\x13\x1b")
            raise ConfigurationError("無法連線到 Telegram")
            yield
        monkeypatch.setattr(terminal, "watch_chats", watch)
        pipe.send_text("\x1b[B\r")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    assert read_dotenv(folder / ".env")["CHANNEL_ID"] == "-10055"


def test_multiple_targets_can_be_checked_searched_and_unchecked(profile):
    manager, folder, values = profile
    registry = ChatRegistry(folder, values["BOT_TOKEN"])
    for number, title in ((-100123, "Zulu 現有群組"), (-1009, "Campus 頻道"), (-1008, "Communications 群組")):
        registry.remember(Chat(id=number, title=title, type="supergroup"))
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 2 + "\r\x1b[B \x01\x0bCommunications\x1b[B \x1b[B \r\x13\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    saved = read_dotenv(folder / ".env")
    assert set(parse_ids(saved["CHANNEL_ID"], "CHANNEL_ID")) == {-1009, -1008}
    assert saved["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"]


def test_multiple_admins_can_be_checked_and_existing_admin_removed(profile):
    manager, folder, values = profile
    registry = ChatRegistry(folder, values["BOT_TOKEN"])
    registry.remember(Chat(id=-100123, title="School", type="supergroup"), administrators=[
        {"id": 42, "name": "Alpha", "username": "alpha"},
        {"id": 84, "name": "Beta", "username": "beta"},
        {"id": 126, "name": "Charlie", "username": ""},
    ])
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 3 + "\r\x1b[B \x1b[B \x1b[B \r\x13\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    assert set(parse_ids(read_dotenv(folder / ".env")["ADMIN_USER_ID"], "ADMIN_USER_ID")) == {84, 126}


def test_admin_fetch_uses_unsaved_token_and_does_not_require_existing_admin(tmp_path, monkeypatch):
    import app.terminal as terminal
    manager = BotManager(tmp_path / "managed")
    folder = manager.create("new")
    font = folder / "font.ttf"
    font.touch()
    values = initial_values(folder / ".env")
    values.update(FONT_REGULAR_PATH=str(font), FONT_BOLD_PATH=str(font))
    (folder / ".env").write_bytes(render_env(None, values))
    with create_pipe_input() as pipe:
        async def fetch(manager_arg, name, token, targets):
            assert token == "9988776655:offline_token" and targets == "-1001"
            assert read_dotenv(folder / ".env")["BOT_TOKEN"] == ""
            pipe.send_text("\x1b[A\r\x1b[B\r\x13\x1b")
            return [{"id": -1001, "title": "School", "type": "supergroup", "administrators": [
                {"id": 84, "name": "Fresh admin", "username": "fresh"},
            ]}], []
        monkeypatch.setattr(terminal, "fetch_administrators", fetch)
        pipe.send_text("\r9988776655:offline_token\r\x1b[B\x1b[B\r-1001\r\x1b[B\x1b[B\r")
        settings_panel(manager, "new", input=pipe, output=DummyOutput())
    saved = read_dotenv(folder / ".env")
    assert saved["BOT_TOKEN"] == "9988776655:offline_token" and saved["ADMIN_USER_ID"] == "84"


def test_long_chinese_bot_name_does_not_hide_status_column():
    row = text_row("機器人" * 10, "執行中", 20)
    assert "…" in row
    from prompt_toolkit.utils import get_cwidth
    assert get_cwidth(row.split("執行中")[0]) == 20
