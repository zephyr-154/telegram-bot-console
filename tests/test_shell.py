from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from app import cli, shell, terminal
from app.manager import BotManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TGBOT_HOME", str(tmp_path / "managed"))
    manager = BotManager()
    manager.create("school")
    manager.create("work")
    return manager


def completions(manager, text):
    return list(shell.CommandCompleter(manager).get_completions(Document(text), CompleteEvent()))


@pytest.mark.parametrize("text", ["tgbot start ", "start ", "/start ", "tgbot stop ", "tgbot restart ", "tgbot config "])
def test_space_automatically_suggests_bot_names_and_status(manager, text):
    choices = completions(manager, text)
    assert [choice.text for choice in choices] == ["school", "work"]
    assert all("待設定" in to_plain_text(choice.display_meta) for choice in choices)


def test_name_completion_filters_prefix_and_refreshes_new_bots(manager):
    assert [x.text for x in completions(manager, "tgbot start SC")] == ["school"]
    manager.create("學校")
    assert [x.text for x in completions(manager, "tgbot start 學")] == ["學校"]
    manager.delete("work")
    assert [x.text for x in completions(manager, "tgbot start ")] == ["school", "學校"]


def test_command_menu_and_arguments_do_not_offer_duplicate_names(manager):
    assert any(x.text == "config " for x in completions(manager, "tgbot "))
    assert [x.text for x in completions(manager, "/con")] == ["/config "]
    assert [x.text for x in completions(manager, "tgbot app ")] == ["delete"]
    for text in ("tgbot add ", "tgbot start school", "tgbot start school "):
        assert completions(manager, text) == []


@pytest.mark.parametrize("text,expected", [
    ('tgbot restore school "C:\\my backups\\copy.sqlite3"', ["restore", "school", "C:\\my backups\\copy.sqlite3"]),
    ('restore school "/my backups/copy.sqlite3"', ["restore", "school", "/my backups/copy.sqlite3"]),
    ("  TGBOT /CONFIG 學校", ["config", "學校"]),
])
def test_shell_preserves_quoted_paths_and_normalizes_commands(text, expected):
    assert shell.command_args(text) == expected


@pytest.mark.parametrize("text", ['restore school "broken', "start\nstop", "start\x1bstop"])
def test_shell_rejects_unmatched_quotes_and_control_characters(text):
    with pytest.raises(ValueError):
        shell.command_args(text)


def test_tgbot_opens_command_shell_and_config_opens_dashboard(manager, monkeypatch):
    opened = []
    monkeypatch.setattr(shell, "command_shell", lambda selected, execute: opened.append(("shell", selected.root)))
    monkeypatch.setattr(terminal, "dashboard", lambda selected, action: opened.append(("dashboard", selected.root)))
    assert cli.main([]) == 0
    assert cli.main(["config"]) == 0
    assert opened == [("shell", manager.root), ("dashboard", manager.root)]


def test_shell_keeps_running_after_help_and_argument_errors(manager, monkeypatch, capsys):
    lines = iter(["tgbot --help", "not-a-command", "tgbot", "tgbot names", "exit"])
    monkeypatch.setattr("builtins.input", lambda message: next(lines))
    shell.command_shell(manager, cli.main)
    result = capsys.readouterr()
    assert "school" in result.out and "work" in result.out
    assert "開啟控制面板" in result.out
    assert "invalid choice" in result.err


def test_full_uninstall_exits_shell_after_scheduling_but_cancel_returns_to_prompt(manager, monkeypatch):
    lines = iter(["tgbot app delete", "tgbot names", "tgbot app delete"])
    monkeypatch.setattr("builtins.input", lambda message: next(lines))
    executed = []
    def execute(args):
        executed.append(args)
        if len(executed) == 3:
            (manager.root / ".uninstalling").touch()
        return 0
    shell.command_shell(manager, execute)
    assert executed == [["app", "delete"], ["names"], ["app", "delete"]]


@pytest.mark.parametrize("keys", ["\x1b[B\r", "\t"])
def test_enter_or_tab_fills_completion_before_executing(manager, keys):
    screen = io.StringIO()
    output = Vt100_Output(screen, lambda: Size(rows=24, columns=100), term="xterm-256color", enable_cpr=False)
    async def drive(pipe):
        session = shell.command_session(manager, input=pipe, output=output)
        task = asyncio.create_task(session.prompt_async())
        try:
            pipe.send_text("tgbot start ")
            for _ in range(100):
                if session.default_buffer.complete_state:
                    break
                await asyncio.sleep(.01)
            assert [x.text for x in session.default_buffer.complete_state.completions] == ["school", "work"]
            pipe.send_text(keys)
            await asyncio.sleep(.1)
            assert not task.done()
            assert session.default_buffer.text == "tgbot start school"
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 2) == "tgbot start school"
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    with create_pipe_input() as pipe:
        asyncio.run(drive(pipe))
    assert "school" in screen.getvalue() and "work" in screen.getvalue()


@pytest.mark.parametrize("text", ["exit\r", "\x03", "\x04"])
def test_leaving_shell_does_not_stop_background_bots(manager, text):
    calls = []
    with create_pipe_input() as pipe:
        pipe.send_text(text)
        shell.command_shell(manager, lambda args: calls.append(args), input=pipe, output=DummyOutput())
    assert calls == []
