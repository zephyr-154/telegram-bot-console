from __future__ import annotations

import sys
import builtins
import time
from collections.abc import Callable
from pathlib import Path

import psutil
from prompt_toolkit import Application, prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from app import __version__
from app.config import ConfigurationError, read_dotenv
from app.manager import BotManager
from app.settings_editor import FIELDS, initial_values, save_config, validate_field


STYLE = Style.from_dict({
    "": "bg:#0c0c0c #dedede", "frame.border": "#525252", "frame.label": "#a3a3a3",
    "section": "#ffb181 bold", "selected": "bg:#34302b #ffffff bold",
    "muted": "#929292", "error": "#ff8585", "input": "bg:#252525 #ffffff",
})
LABELS = {"running": "執行中", "starting": "啟動中", "stopping": "停止中", "stopped": "已停止", "failed": "異常停止", "unconfigured": "待設定"}


def text_row(label: str, value: str = "", width: int = 32) -> str:
    if get_cwidth(label) > width - 2:
        while get_cwidth(label) > width - 3:
            label = label[:-1]
        label += "…"
    return label + " " * max(2, width - get_cwidth(label)) + value


def select(title: str, options, *, input=None, output=None) -> str | None:
    def rows():
        return options() if callable(options) else options
    if input is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        entries = rows()
        print(f"\n{title}")
        for index, (_, label) in enumerate(entries, 1):
            print(f" {index}. {label}")
        answer = builtins.input("選擇編號（0 返回）：")
        if answer.strip().isdigit() and 1 <= int(answer) <= len(entries):
            return entries[int(answer) - 1][0]
        return None
    index = 0
    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("k")
    def up(event):
        nonlocal index
        index = max(0, index - 1)

    @bindings.add("down")
    @bindings.add("j")
    def down(event):
        nonlocal index
        index = min(len(rows()) - 1, index + 1)

    @bindings.add("enter")
    def enter(event):
        entries = rows()
        event.app.exit(result=entries[min(index, len(entries) - 1)][0] if entries else None)

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("q")
    def close(event):
        event.app.exit(result=None)

    def content():
        entries = rows()
        return [("class:selected" if pos == min(index, len(entries) - 1) else "", (" › " if pos == index else "   ") + label + "\n") for pos, (_, label) in enumerate(entries)]

    control = FormattedTextControl(content, focusable=True, get_cursor_position=lambda: Point(x=0, y=index))
    body = HSplit([Window(FormattedTextControl([("class:section", "  " + title)]), height=2),
                   Window(control, wrap_lines=False), Window(FormattedTextControl([("class:muted", " ↑↓ 移動 · Enter 選擇 · Esc 返回 · q 離開")]), height=1)])
    app = Application(layout=Layout(Frame(body, title=f" TGBOT {__version__} "), control), style=STYLE,
                      key_bindings=bindings, full_screen=True, refresh_interval=1, input=input, output=output)
    return app.run()


def select_bot(manager: BotManager, name: str | None, *, title: str = "選擇機器人") -> str | None:
    names = manager.names()
    if name is not None:
        exact = [item for item in names if item.casefold() == name.casefold()]
        if exact:
            return exact[0]
        names = [item for item in names if name.casefold() in item.casefold()]
        if not names:
            raise ConfigurationError("沒有符合名稱的機器人。可用名稱：" + ("、".join(manager.names()) or "尚無"))
    if not names:
        raise ConfigurationError("尚無機器人，請先使用 tgbot add")
    return select(title, [(item, item) for item in names])


def name_prompt(manager: BotManager) -> str:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return prompt("機器人名稱：", completer=WordCompleter(manager.names(), ignore_case=True))
    return builtins.input("機器人名稱：")


def settings_panel(manager: BotManager, name: str, *, input=None, output=None) -> str | None:
    folder = manager.folder(name)
    path = folder / ".env"
    if input is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        from app.settings_editor import interactive_menu
        interactive_menu(path, saver=lambda updated, before: manager.save(name, updated, before))
        return
    original = path.read_bytes()
    values = initial_values(path)
    actions = {"start": "背景啟動", "stop": "停止機器人", "restart": "重啟機器人", "backup": "立即備份", "logs": "查看最近日誌", "delete": "刪除機器人"}
    keys = list(FIELDS) + list(actions)
    index, editing, dirty, message = 0, False, False, ""
    message_style = "class:muted"
    field = TextArea(multiline=False, height=1, style="class:input", password=Condition(lambda: keys[index] == "BOT_TOKEN"))
    bindings = KeyBindings()
    app = None

    def apply_edit():
        nonlocal editing, dirty, message, message_style
        key = keys[index]
        value = field.text.strip()
        if key == "BOT_TOKEN" and not value:
            editing = False
            app.layout.focus(control)
            return
        if key.endswith("_PATH") and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        try:
            validate_field(key, value, folder)
            dirty |= values[key] != value
            values[key] = value
            message, editing = "已修改，Ctrl+S 儲存", False
            message_style = "class:muted"
            app.layout.focus(control)
        except ConfigurationError as exc:
            message = str(exc)
            message_style = "class:error"

    @bindings.add("up", filter=Condition(lambda: not editing))
    def up(event):
        nonlocal index
        index = max(0, index - 1)

    @bindings.add("down", filter=Condition(lambda: not editing))
    def down(event):
        nonlocal index
        index = min(len(keys) - 1, index + 1)

    @bindings.add("enter")
    def edit(event):
        nonlocal editing, message, message_style
        if editing:
            apply_edit()
            return
        key = keys[index]
        if key in actions:
            if dirty:
                message = "請先 Ctrl+S 儲存修改，再執行這個操作"
                return
            event.app.exit(result=key)
            return
        field.buffer.document = Document("" if key == "BOT_TOKEN" else values[key])
        field.buffer.cursor_position = len(field.text)
        message, editing = "Token 留空保留原值" if key == "BOT_TOKEN" else "Enter 套用此欄位；Esc 取消", True
        message_style = "class:muted"
        event.app.layout.focus(field)

    @bindings.add("c-s")
    def save(event):
        nonlocal original, dirty, message, message_style
        if editing:
            apply_edit()
            if editing:
                return
        try:
            manager.save(name, values, original)
            original, dirty, message = path.read_bytes(), False, "設定已儲存；執行中的機器人需重啟才會套用"
            message_style = "class:muted"
        except (ConfigurationError, OSError) as exc:
            message = str(exc) if isinstance(exc, ConfigurationError) else "儲存失敗，原設定仍保留"
            message_style = "class:error"

    @bindings.add("escape")
    def back(event):
        nonlocal editing
        if editing:
            editing = False
            event.app.layout.focus(control)
        elif dirty:
            event.app.exit()
        else:
            event.app.exit()

    @bindings.add("c-c")
    def close(event):
        event.app.exit()

    def content():
        result = [("class:section", "  基本設定\n")]
        for pos, key in enumerate(FIELDS):
            value = "••••••••（已設定）" if key == "BOT_TOKEN" and values[key] else values[key] or "（未設定）"
            result.append(("class:selected" if pos == index else "", (" › " if pos == index else "   ") + text_row(FIELDS[key], value) + "\n"))
        result.append(("class:section", "\n  操作\n"))
        for pos, (key, label) in enumerate(actions.items(), len(FIELDS)):
            result.append(("class:selected" if pos == index else "", (" › " if pos == index else "   ") + label + "\n"))
        result.extend([("class:section", "\n  資料\n"), ("class:muted", "   " + str(folder) + "\n   每台獨立保存設定、資料庫、備份與日誌。")])
        return result

    control = FormattedTextControl(content, focusable=True, get_cursor_position=lambda: Point(x=0, y=index + 1 + (2 if index >= len(FIELDS) else 0)))
    body = HSplit([Window(control), ConditionalContainer(field, filter=Condition(lambda: editing)),
                   Window(FormattedTextControl(lambda: [(message_style, " " + message)]), height=2),
                   Window(FormattedTextControl([("class:muted", " ↑↓ 移動 · Enter 編輯 · Ctrl+S 儲存 · Esc 返回／放棄未儲存修改")]), height=1)])
    app = Application(layout=Layout(Frame(body, title=f" {name} · 設定 "), control), style=STYLE,
                      key_bindings=bindings, full_screen=True, input=input, output=output)
    return app.run()


def dashboard(manager: BotManager, action: Callable[[str, str | None], None]) -> None:
    manager.ensure_root()
    def rows():
        entries = []
        for name in manager.names():
            info = manager.status(name)
            detail = f"{LABELS.get(info['status'], info['status'])}   PID {info['pid'] or '—'}   {info['memory_mb']:.1f} MB"
            entries.append(("bot:" + name, text_row(name, detail)))
        memory = psutil.virtual_memory()
        entries += [("add", "＋ 新增機器人"), ("start", "啟動全部機器人"), ("stop", "停止全部機器人"),
                    ("restart", "重啟全部機器人"), ("refresh", f"重新整理   主機記憶體 {memory.percent}%"), ("quit", "離開控制面板（機器人繼續背景執行）")]
        return entries
    while True:
        selected = select("機器人                         運行狀態 / PID / 記憶體", rows)
        if selected in {None, "quit"}:
            return
        if selected == "refresh":
            continue
        try:
            if selected.startswith("bot:"):
                name = selected[4:]
                while True:
                    choice = settings_panel(manager, name)
                    if choice is None:
                        break
                    action(choice, name)
                    if choice in {"logs", "backup"} and sys.stdin.isatty():
                        prompt("Enter 返回設定選單")
                    if choice == "delete" and name not in manager.names():
                        break
            else:
                action(selected, None)
        except (ConfigurationError, OSError, RuntimeError) as exc:
            print(str(exc) if not isinstance(exc, OSError) else "檔案操作失敗，請確認權限")
            if sys.stdin.isatty():
                prompt("Enter 返回控制面板")
