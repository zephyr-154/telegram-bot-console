from __future__ import annotations

import builtins
import shlex
import sys
from collections.abc import Callable
from io import StringIO

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.plain_text import PlainTextOutput
from prompt_toolkit.styles import Style, merge_styles

from app import __version__
from app.config import ConfigurationError
from app.manager import BotManager
from app.terminal import LABELS, STYLE


COMMANDS = {
    "config": "開啟控制面板；加名稱設定指定機器人",
    "add": "新增機器人，並開啟設定",
    "start": "背景啟動全部或指定機器人",
    "stop": "停止全部或指定機器人",
    "restart": "重啟全部或指定機器人",
    "status": "查看運行狀態、PID 與記憶體",
    "list": "列出所有機器人的狀態",
    "names": "列出機器人名稱",
    "check": "檢查本機設定",
    "logs": "查看指定機器人的最近日誌",
    "backup": "備份指定機器人",
    "restore": "還原：restore 名稱 備份檔案",
    "delete": "刪除指定機器人，操作前會確認",
    "app": "整個應用程式管理：app delete 完整解除安裝",
    "help": "顯示指令說明",
    "exit": "離開指令介面，機器人繼續背景執行",
}
BOT_COMMANDS = {"config", "start", "stop", "restart", "status", "check", "logs", "backup", "restore", "delete"}
SHELL_STYLE = merge_styles([STYLE, Style.from_dict({
    "prompt": "#ffb181 bold", "placeholder": "#777777", "bottom-toolbar": "bg:#0c0c0c #929292",
    "completion-menu.completion": "bg:#171717 #ffb181",
    "completion-menu.completion.current": "bg:#49372b #ffffff bold",
    "completion-menu.meta.completion": "bg:#171717 #b0b0b0",
    "completion-menu.meta.completion.current": "bg:#49372b #ffffff",
})])
BANNER = r"""
  _____ ____ ____   ___ _____
 |_   _/ ___| __ ) / _ \_   _|
   | || |  _|  _ \| | | || |
   | || |_| | |_) | |_| || |
   |_| \____|____/ \___/ |_|
"""


def command_args(text: str) -> list[str]:
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("一次只能輸入一行指令")
    lexer = shlex.shlex(StringIO(text), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""  # Windows 路徑中的反斜線是路徑本身。
    args = list(lexer)
    if args and args[0].casefold() == "tgbot":
        args.pop(0)
    if args:
        args[0] = args[0].removeprefix("/").casefold()
    return args


class CommandCompleter(Completer):
    def __init__(self, manager: BotManager):
        self.manager = manager

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        words = before.split()
        if words and words[0].casefold() == "tgbot" and (len(words) > 1 or before.endswith(" ")):
            before = before.lstrip()[len(words[0]):].lstrip()
            words = before.split()
        elif len(words) == 1 and not before.endswith(" ") and "tgbot".startswith(words[0].casefold()):
            yield Completion("tgbot ", start_position=-len(words[0]), display="tgbot", display_meta="顯示全部指令")
            return
        if not words or (len(words) == 1 and not before.endswith(" ")):
            word = words[0] if words else ""
            slash = "/" if word.startswith("/") else ""
            prefix = word.removeprefix("/").casefold()
            for command, description in COMMANDS.items():
                if command.startswith(prefix):
                    yield Completion(slash + command + " ", start_position=-len(word),
                                     display="tgbot " + command, display_meta=description)
            return
        command = words[0].removeprefix("/").casefold()
        if len(words) > 2 or (len(words) == 2 and before.endswith(" ")):
            return
        prefix = words[1] if len(words) == 2 else ""
        if command == "app":
            if "delete".startswith(prefix):
                yield Completion("delete", start_position=-len(prefix), display_meta="完整解除安裝，操作前會確認")
        elif command in BOT_COMMANDS:
            try:
                for name in self.manager.names():
                    if name.casefold().startswith(prefix.casefold()) and name != prefix:
                        status = self.manager.status(name)["status"]
                        yield Completion(name, start_position=-len(prefix), display_meta="機器人 · " + LABELS.get(status, status))
            except (ConfigurationError, OSError):
                return


def command_session(manager: BotManager, *, input=None, output=None) -> PromptSession:
    bindings = KeyBindings()

    @bindings.add("tab")
    def tab(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and state.completions:
            buffer.apply_completion(state.current_completion or state.completions[0])
        else:
            buffer.start_completion()

    @bindings.add("enter")
    def enter(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and state.current_completion:
            buffer.apply_completion(state.current_completion)
        else:
            buffer.validate_and_handle()

    return PromptSession(
        FormattedText([("class:prompt", "tgbot"), ("class:muted", " Ready> ")]),
        completer=CommandCompleter(manager), complete_while_typing=True, reserve_space_for_menu=8,
        history=InMemoryHistory(), key_bindings=bindings, style=SHELL_STYLE, show_frame=True,
        placeholder="輸入 tgbot config 開啟控制面板，或輸入指令…",
        bottom_toolbar=" ↑↓ 選擇 · Tab / Enter 帶入 · Enter 執行 · Ctrl+C / exit 離開",
        input=input, output=output,
    )


def command_shell(manager: BotManager, execute: Callable[[list[str]], int], *, input=None, output=None) -> None:
    manager.ensure_root()
    interactive = input is not None or (sys.stdin.isatty() and sys.stdout.isatty())
    session = command_session(manager, input=input, output=output) if interactive else None
    if not interactive and output is None:
        output = PlainTextOutput(sys.stdout)
    print_formatted_text(FormattedText([
        ("class:section", BANNER), ("", f" Telegram Bot Console v{__version__}\n"),
        ("class:muted", " 輸入 tgbot config 開啟控制面板；tgbot start 後加空格即可選擇名稱。\n help 查看指令；exit 離開，機器人繼續背景執行。\n"),
    ]), style=SHELL_STYLE, output=output)
    while True:
        try:
            text = session.prompt() if session else builtins.input("tgbot Ready> ")
            if not text.strip():
                continue
            args = command_args(text)
            if args in (["exit"], ["quit"], ["0"]):
                return
            if not args or args == ["help"]:
                print_formatted_text(FormattedText([
                    part for command, description in COMMANDS.items()
                    for part in [("class:section", f"  tgbot {command:<9}"), ("class:muted", description + "\n")]
                ]), style=SHELL_STYLE, output=output)
                continue
            try:
                result = execute(args)
            except SystemExit:
                continue  # argparse 的說明或錯誤不能結束整個指令介面。
            if result == 0 and args[:2] == ["app", "delete"] and (manager.root / ".uninstalling").exists():
                return
        except ValueError:
            print("指令格式錯誤：請使用成對引號，一次輸入一行指令。", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            print("\n已離開指令介面；背景機器人維持原狀。")
            return
