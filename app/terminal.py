from __future__ import annotations

import sys
import builtins
import asyncio
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
from app.chat_discovery import CHAT_LABELS, administrator_candidates, cached_chats, fetch_administrators, watch_chats
from app.config import ConfigurationError, parse_ids, read_dotenv
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
    basic_keys = ["BOT_TOKEN", "discover", "CHANNEL_ID", "ADMIN_USER_ID", "fetch_admins",
                  *(key for key in FIELDS if key not in {"BOT_TOKEN", "CHANNEL_ID", "ADMIN_USER_ID"})]
    keys = basic_keys + list(actions)
    index, editing, dirty, message = 0, False, False, ""
    chats = cached_chats(folder, values["BOT_TOKEN"])
    discovery_task = None
    admins_task = None
    discovery_notice = ""
    selection_index, selection_original = 0, ""
    selected_ids, selection_changed = set(), False
    cursor_rows = {}
    message_style = "class:muted"
    field = TextArea(multiline=False, height=1, style="class:input", password=Condition(lambda: keys[index] == "BOT_TOKEN"))
    bindings = KeyBindings()
    app = None

    def configured_ids(key):
        try:
            return parse_ids(values[key], key)
        except ConfigurationError:
            return ()

    def id_choices():
        key = keys[index]
        if key == "CHANNEL_ID":
            choices = list(chats)
        else:
            choices = [{**person, "title": person["name"], "type": "administrator"}
                       for person in administrator_candidates(chats, configured_ids("CHANNEL_ID"))]
        known = {choice["id"] for choice in choices}
        for number in (*configured_ids(key), *sorted(selected_ids)):
            if number not in known:
                choices.append({"id": number, "title": "手動設定", "type": "manual"})
                known.add(number)
        query = "" if field.text == selection_original or any(mark in field.text for mark in ",，") else field.text.strip().casefold()
        return sorted((choice for choice in choices if query in choice["title"].casefold()
                       or query in str(choice["id"]) or query in choice.get("username", "").casefold()),
                      key=lambda choice: (choice["title"].casefold(), choice["id"]))

    def input_changed(_):
        nonlocal selection_index, selection_changed
        selection_index = 0
        selection_changed = False

    field.buffer.on_text_changed += input_changed

    def replace_chats(found):
        nonlocal chats, selection_index
        choices = id_choices() if editing and keys[index] in {"CHANNEL_ID", "ADMIN_USER_ID"} else []
        selected_id = choices[selection_index - 1]["id"] if 0 < selection_index <= len(choices) else None
        chats = found
        if selected_id is not None:
            selection_index = next((number for number, choice in enumerate(id_choices(), 1)
                                    if choice["id"] == selected_id), 0)

    async def discover(token):
        nonlocal discovery_notice
        try:
            async for found, notice in watch_chats(manager, name, token, values["CHANNEL_ID"]):
                replace_chats(found)
                discovery_notice = notice
                app.invalidate()
        except ConfigurationError as exc:
            discovery_notice = str(exc)
        except (OSError, RuntimeError):
            discovery_notice = "無法讀寫群組清單，請確認資料目錄權限或稍後重試"

    def discovery_done(task):
        nonlocal discovery_task
        if discovery_task is task:
            discovery_task = None
        app.invalidate()

    async def load_administrators(token, targets):
        nonlocal message, message_style
        try:
            found, failed = await fetch_administrators(manager, name, token, targets)
            if values["BOT_TOKEN"] != token or values["CHANNEL_ID"] != targets:
                message = "設定已變更，請重新拉取管理員名單"
                return
            replace_chats(found)
            count = len(administrator_candidates(chats, configured_ids("CHANNEL_ID")))
            message = f"已記錄 {count} 位候選，請到管理員欄位選取；儲存後才會授權"
            if failed:
                message += f"；{len(failed)} 個目標無法讀取，仍可能顯示已記錄資料"
            message_style = "class:muted"
        except ConfigurationError as exc:
            message, message_style = str(exc), "class:error"
        except (OSError, RuntimeError):
            message, message_style = "無法更新管理員清單，請稍後重試", "class:error"

    def administrators_done(task):
        nonlocal admins_task
        if admins_task is task:
            admins_task = None
        app.invalidate()

    def apply_edit():
        nonlocal editing, dirty, message, message_style, chats, discovery_notice
        key = keys[index]
        if key in {"CHANNEL_ID", "ADMIN_USER_ID"} and selection_index and not selection_changed:
            choices = id_choices()
            if selection_index <= len(choices):
                field.text = str(choices[selection_index - 1]["id"])
        value = field.text.strip()
        if key == "BOT_TOKEN" and not value:
            editing = False
            app.layout.focus(control)
            return
        if key.endswith("_PATH") and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        try:
            validate_field(key, value, folder)
            if key in {"CHANNEL_ID", "ADMIN_USER_ID"}:
                value = ",".join(map(str, parse_ids(value, key)))
            if key == "BOT_TOKEN" and values[key] != value:
                if discovery_task is not None:
                    discovery_task.cancel()
                if admins_task is not None:
                    admins_task.cancel()
                chats = cached_chats(folder, value)
                discovery_notice = "Token 已變更，請重新開始偵測"
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

    @bindings.add("up", filter=Condition(lambda: editing and keys[index] in {"CHANNEL_ID", "ADMIN_USER_ID"}))
    def previous_chat(event):
        nonlocal selection_index
        selection_index = max(0, selection_index - 1)

    @bindings.add("down", filter=Condition(lambda: editing and keys[index] in {"CHANNEL_ID", "ADMIN_USER_ID"}))
    def next_chat(event):
        nonlocal selection_index
        selection_index = min(len(id_choices()), selection_index + 1)

    @bindings.add(" ", filter=Condition(lambda: editing and keys[index] in {"CHANNEL_ID", "ADMIN_USER_ID"} and selection_index > 0))
    def toggle_id(event):
        nonlocal selection_index, selection_original, selection_changed
        choices = id_choices()
        if selection_index > len(choices):
            return
        number = choices[selection_index - 1]["id"]
        if number in selected_ids:
            selected_ids.remove(number)
        else:
            selected_ids.add(number)
        field.text = ",".join(map(str, sorted(selected_ids)))
        field.buffer.cursor_position = len(field.text)
        selection_original, selection_changed = field.text, True
        selection_index = next((pos for pos, choice in enumerate(id_choices(), 1) if choice["id"] == number), 0)

    @bindings.add("enter")
    def edit(event):
        nonlocal editing, message, message_style, discovery_task, discovery_notice, admins_task
        nonlocal selection_index, selection_original, selected_ids, selection_changed
        if editing:
            apply_edit()
            return
        key = keys[index]
        if key == "fetch_admins":
            if admins_task is not None:
                return
            try:
                validate_field("BOT_TOKEN", values["BOT_TOKEN"], folder)
                validate_field("CHANNEL_ID", values["CHANNEL_ID"], folder)
            except ConfigurationError as exc:
                message, message_style = str(exc), "class:error"
                return
            message, message_style = "正在讀取所選群組／頻道的管理員…", "class:muted"
            admins_task = event.app.create_background_task(load_administrators(values["BOT_TOKEN"], values["CHANNEL_ID"]))
            admins_task.add_done_callback(administrators_done)
            return
        if key == "discover":
            if discovery_task is not None:
                discovery_task.cancel()
                discovery_notice = "偵測已停止；已記錄的清單保留"
            else:
                try:
                    validate_field("BOT_TOKEN", values["BOT_TOKEN"], folder)
                except ConfigurationError as exc:
                    message, message_style = str(exc), "class:error"
                    return
                discovery_notice = "正在連線到 Telegram…"
                discovery_task = event.app.create_background_task(discover(values["BOT_TOKEN"]))
                discovery_task.add_done_callback(discovery_done)
            return
        if key in actions:
            if dirty:
                message = "請先 Ctrl+S 儲存修改，再執行這個操作"
                return
            event.app.exit(result=key)
            return
        field.buffer.document = Document("" if key == "BOT_TOKEN" else values[key])
        field.buffer.cursor_position = len(field.text)
        if key in {"CHANNEL_ID", "ADMIN_USER_ID"}:
            selection_index, selection_original = 0, field.text
            selected_ids, selection_changed = set(configured_ids(key)), False
        message, editing = "Token 留空保留原值" if key == "BOT_TOKEN" else "Enter 套用此欄位；Esc 取消", True
        if key in {"CHANNEL_ID", "ADMIN_USER_ID"}:
            message = "ID 用逗號分隔／名稱搜尋 · ↑↓ 選擇 · Space 多選 · Enter 單選／套用 · Esc 取消"
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
        nonlocal selection_index
        result = [("class:section", "  基本設定\n")]
        row = 1
        for pos, key in enumerate(basic_keys):
            cursor_rows[pos] = row
            if key == "discover":
                label = "偵測群組／頻道"
                value = ("偵測中 · Enter 停止" if discovery_task is not None else "Enter 開始偵測") + f" · 已記錄 {len(chats)} 個"
            elif key == "fetch_admins":
                label = "拉取群組管理員"
                count = len(administrator_candidates(chats, configured_ids("CHANNEL_ID")))
                value = "讀取中…" if admins_task is not None else f"Enter 讀取 · 已記錄 {count} 位候選"
            else:
                label = FIELDS[key]
                value = "••••••••（已設定）" if key == "BOT_TOKEN" and values[key] else values[key] or "（未設定）"
                if key in {"CHANNEL_ID", "ADMIN_USER_ID"} and not editing:
                    ids = configured_ids(key)
                    if len(ids) > 1:
                        unit = "個" if key == "CHANNEL_ID" else "位"
                        value = f"已選 {len(ids)} {unit} · Enter 查看／修改"
                    elif key == "CHANNEL_ID":
                        chosen = next((chat for chat in chats if str(chat["id"]) == values[key]), None)
                        if chosen:
                            value = f"{chosen['title']}  {values[key]}"
            result.append(("class:selected" if pos == index else "", (" › " if pos == index else "   ") + text_row(label, value) + "\n"))
            row += 1
            if key in {"CHANNEL_ID", "ADMIN_USER_ID"} and editing and keys[index] == key:
                choices = id_choices()
                selection_index = min(selection_index, len(choices))
                result.append(("class:selected" if selection_index == 0 else "class:muted", "     手動輸入 ID（逗號分隔）／輸入名稱搜尋\n"))
                row += 1
                start = max(0, selection_index - 6)
                for number, chat in enumerate(choices[start:start + 6], start + 1):
                    selected = number == selection_index
                    label = ("› " if selected else "  ") + ("[x] " if chat["id"] in selected_ids else "[ ] ") + chat["title"]
                    detail = f"{chat['id']} · {CHAT_LABELS.get(chat['type'], '手動設定')}"
                    if key == "ADMIN_USER_ID" and chat["type"] != "manual":
                        account = " @" + chat["username"] if chat.get("username") else ""
                        detail = f"{chat['id']}{account} · {'／'.join(chat['sources'])}"
                    result.append(("class:selected" if selected else "class:muted",
                                   "     " + text_row(label, detail) + "\n"))
                    if selected:
                        cursor_rows[pos] = row
                    row += 1
                if not choices:
                    hint = "先開始偵測並邀請機器人" if key == "CHANNEL_ID" else "先開始偵測或拉取群組管理員"
                    result.append(("class:muted", f"     尚無符合項目；可手動輸入，或{hint}\n"))
                    row += 1
                elif len(choices) > 6:
                    result.append(("class:muted", f"     共 {len(choices)} 個，↑↓ 瀏覽更多\n"))
                    row += 1
        result.append(("class:section", "\n  操作\n"))
        row += 2
        for pos, (key, label) in enumerate(actions.items(), len(basic_keys)):
            cursor_rows[pos] = row
            result.append(("class:selected" if pos == index else "", (" › " if pos == index else "   ") + label + "\n"))
            row += 1
        result.extend([("class:section", "\n  資料\n"), ("class:muted", "   " + str(folder) + "\n   每台獨立保存設定、資料庫、備份與日誌。")])
        return result

    control = FormattedTextControl(content, focusable=True, get_cursor_position=lambda: Point(x=0, y=cursor_rows.get(index, 1)))
    body = HSplit([Window(control), ConditionalContainer(field, filter=Condition(lambda: editing)),
                   Window(FormattedTextControl(lambda: [("class:muted", " " + discovery_notice)]), height=2, wrap_lines=True),
                   Window(FormattedTextControl(lambda: [(message_style, " " + message)]), height=2, wrap_lines=True),
                   Window(FormattedTextControl([("class:muted", " ↑↓ 移動 · Enter 編輯 · Ctrl+S 儲存 · Esc 返回／放棄未儲存修改")]), height=1)])
    app = Application(layout=Layout(Frame(body, title=f" {name} · 設定 "), control), style=STYLE,
                      key_bindings=bindings, full_screen=True, refresh_interval=1, input=input, output=output)
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
