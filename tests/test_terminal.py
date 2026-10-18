from __future__ import annotations

import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from app.manager import BotManager
from app.settings_editor import initial_values, save_config
from app.config import read_dotenv
from app.terminal import select, settings_panel, text_row


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
        pipe.send_text("\x1b[B\x1b[B\r\x01\x0b84\r\x13\x1b")
        settings_panel(manager, "school", input=pipe, output=DummyOutput())
    saved = read_dotenv(folder / ".env")
    assert saved["ADMIN_USER_ID"] == "84"
    assert saved["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"]
    assert (folder / ".env.bak").read_bytes() == original


def test_settings_escape_discards_unsaved_changes(profile):
    manager, folder, _ = profile
    original = (folder / ".env").read_bytes()
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\x1b[B\r\x01\x0b84\r\x1b")
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
        pipe.send_text("\x1b[B" * 7 + "\r")
        assert settings_panel(manager, "school", input=pipe, output=DummyOutput()) == "start"


def test_chinese_labels_align_using_display_width():
    assert text_row("管理員", "42", 12) == "管理員" + " " * 6 + "42"


def test_long_chinese_bot_name_does_not_hide_status_column():
    row = text_row("機器人" * 10, "執行中", 20)
    assert "…" in row
    from prompt_toolkit.utils import get_cwidth
    assert get_cwidth(row.split("執行中")[0]) == 20
