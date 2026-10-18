from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from aiogram.utils.token import TokenValidationError
from aiogram.exceptions import TelegramAPIError

from app import __version__
from app.config import ConfigurationError, default_env_path, read_dotenv


from app.settings_editor import (
    DEFAULTS, FIELDS, edit_field, initial_values, interactive_menu,
    render_env, save_config, validate_field, validate_values,
)
from app.storage import atomic_write as _atomic_write

PROJECT_DIR = Path(__file__).resolve().parent.parent


def install_windows_command() -> Path:
    if sys.platform != "win32":
        raise ConfigurationError("此安裝選項適用 Windows；Linux 請使用 python -m app.cli")
    if not (PROJECT_DIR / "tgbot.cmd").is_file():
        raise ConfigurationError("套件版已提供 tgbot 指令，不需要 --install；找不到指令時請執行 uv tool update-shell 並重新開啟終端機")
    import ctypes
    import winreg

    folder = Path(os.environ["LOCALAPPDATA"]) / "TelegramAnonymousBoard" / "bin"
    folder.mkdir(parents=True, exist_ok=True)
    command = folder / "tgbot.cmd"
    target = str(PROJECT_DIR / "tgbot.cmd").replace("%", "%%")
    content = f'@echo off\r\nchcp 65001 >nul\r\n"{target}" %*\r\n'.encode("utf-8")
    if command.exists() and command.read_bytes() != content:
        raise ConfigurationError(f"{command} 已指向另一份專案，請先確認或移走該檔案")
    _atomic_write(command, content)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            user_path, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            user_path, value_type = "", winreg.REG_EXPAND_SZ
        entries = [os.path.normcase(os.path.normpath(os.path.expandvars(part.strip().strip('"')))) for part in user_path.split(";") if part.strip()]
        if os.path.normcase(str(folder)) not in entries:
            updated = user_path.rstrip(";") + (";" if user_path else "") + str(folder)
            winreg.SetValueEx(key, "Path", 0, value_type, updated)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    return command


def _confirm(message: str, expected: str, yes: bool) -> bool:
    if yes:
        return True
    print(message)
    return input(f"輸入 {expected} 確認，Enter 取消：").strip() == expected


def _legacy(command: str, path: Path, backup_file: Path | None = None, *, use_environment: bool = True) -> int:
    if command == "config":
        return interactive_menu(path)
    if command == "check":
        if not path.is_file():
            raise ConfigurationError("尚未建立設定檔，請執行 tgbot 完成設定")
        validate_values(read_dotenv(path), path.parent)
        print("設定檔檢查通過；未連線驗證 Token 或群組權限。")
    elif command == "run":
        from app.main import main as run_bot
        run_bot(env_path=path)
    elif command in {"backup", "restore"}:
        from app.backup import main as backup_main
        options = {} if use_environment else {"use_environment": False}
        backup_main(["create"] if command == "backup" else ["restore", str(backup_file)], env_path=path, **options)
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="舊版單機器人設定檔；多機器人請用 config 名稱")
    parser = argparse.ArgumentParser(prog="tgbot", parents=[common], description="多機器人 CLI 控制面板；方向鍵選擇、Enter 操作")
    parser.add_argument("--version", action="version", version=f"tgbot {__version__}")
    parser.add_argument("--install", action="store_true", help="原始碼版 Windows 指令捷徑")
    parser.add_argument("--check", action="store_true", help="檢查舊版單機器人設定")
    commands = parser.add_subparsers(dest="command")
    add = commands.add_parser("add", help="新增機器人")
    add.add_argument("name", nargs="?")
    add.add_argument("--no-config", action="store_true", help="只建立設定草稿")
    for command, help_text in [("config", "設定機器人"), ("check", "檢查機器人設定"), ("start", "背景啟動全部或指定機器人"),
                               ("stop", "停止全部或指定機器人"), ("restart", "重啟全部或指定機器人"), ("status", "查看運行狀態"),
                               ("backup", "備份指定機器人"), ("logs", "查看指定機器人日誌"), ("delete", "刪除指定機器人")]:
        sub = commands.add_parser(command, help=help_text, parents=[common])
        sub.add_argument("name", nargs="?", help="完整名稱，或輸入部分名稱開啟選單")
        sub.add_argument("--select", action="store_true", help="使用方向鍵選擇機器人")
        if command == "delete":
            sub.add_argument("--yes", action="store_true", help="確認永久刪除，不再詢問")
    commands.add_parser("list", help="列出機器人及狀態")
    commands.add_parser("names", help="只列出可用名稱")
    commands.add_parser("run", help="舊版單機器人前景執行", parents=[common])
    restore = commands.add_parser("restore", help="還原：restore 名稱 備份檔案", parents=[common])
    restore.add_argument("name_or_backup", nargs="?")
    restore.add_argument("backup_file", nargs="?", type=Path)
    app_parser = commands.add_parser("app", help="管理整個應用程式")
    app_commands = app_parser.add_subparsers(dest="app_command", required=True)
    remove = app_commands.add_parser("delete", help="停止所有機器人並完整解除安裝（uv 套件版）")
    remove.add_argument("--yes", action="store_true", help="確認刪除全部私人資料並解除安裝")
    args = parser.parse_args(argv)
    explicit_config = getattr(args, "config", None)
    if args.command == "restore" and not args.name_or_backup:
        parser.error("restore 必須指定備份檔案")
    if args.check and args.command is not None:
        parser.error("--check 請單獨使用，或改用 tgbot check")
    if args.install and args.command is not None:
        parser.error("--install 不可與其他指令同時使用")
    if explicit_config and getattr(args, "name", None):
        parser.error("--config 檔案與機器人名稱不可同時使用")
    if explicit_config and args.command == "restore" and args.backup_file is not None:
        parser.error("舊版 --config 還原只接受一個備份檔案，不接受機器人名稱")
    if args.command == "start" and explicit_config:
        parser.error("前景執行請改用 tgbot run --config 檔案；多機器人背景啟動使用 tgbot start 名稱")
    try:
        if args.install:
            command = install_windows_command()
            print(f'已安裝捷徑。目前 CMD 可先執行：set "PATH={command.parent};%PATH%"')
            return 0
        if args.check or explicit_config or args.command == "run":
            path = (explicit_config or default_env_path()).expanduser().resolve()
            command = "check" if args.check else args.command or "config"
            if command not in {"config", "check", "run", "backup", "restore"}:
                parser.error("此指令不接受 --config，請指定機器人名稱")
            return _legacy(command, path, Path(args.name_or_backup).expanduser() if command == "restore" else None)

        from app.manager import BotManager
        from app.terminal import LABELS, dashboard, name_prompt, select_bot, settings_panel
        manager = BotManager()

        def action(command: str, name: str | None = None) -> None:
            if command == "add":
                name = name or name_prompt(manager)
                manager.create(name)
                print(f"已新增「{name}」。請填寫 Token、群組及管理員 ID。")
                if sys.stdin.isatty() and not getattr(args, "no_config", False):
                    action("config", name)
                return
            if command in {"start", "stop", "restart"} and name is None:
                if not manager.all(command):
                    raise ConfigurationError("部分機器人操作失敗，請查看上述原因")
                return
            if command in {"status", "list", "names", "check"} and name is None:
                names = manager.names()
                if not names:
                    print("尚無機器人，請使用 tgbot add 新增。")
                    return
                for item in names:
                    if command == "names":
                        print(item)
                    elif command == "check":
                        manager.validate_values(item, read_dotenv(manager.folder(item) / ".env"))
                        print(f"{item}：設定檢查通過")
                    else:
                        state = manager.status(item)
                        print(f"{item}  {LABELS.get(state['status'], state['status'])}  PID {state['pid'] or '—'}  {state['memory_mb']:.1f} MB  {state.get('error', '')}")
                return
            selected = select_bot(manager, name)
            if selected is None:
                return
            folder = manager.folder(selected)
            if command == "config":
                while selected in manager.names():
                    operation = settings_panel(manager, selected)
                    if operation is None:
                        break
                    action(operation, selected)
                    if operation in {"logs", "backup"} and sys.stdin.isatty():
                        input("Enter 返回設定選單")
            elif command == "check":
                manager.validate_values(selected, read_dotenv(folder / ".env"))
                print(f"{selected}：設定檢查通過")
            elif command in {"start", "stop"}:
                print(f"{selected}：{getattr(manager, command)(selected)}")
            elif command == "restart":
                manager.stop(selected)
                print(f"{selected}：{manager.start(selected)}")
            elif command == "delete":
                if _confirm(f"永久刪除「{selected}」的設定、資料庫、備份和日誌；群組歷史訊息不會刪除。", selected, getattr(args, "yes", False)):
                    manager.delete(selected)
                    print(f"已刪除「{selected}」。")
            elif command == "backup":
                _legacy("backup", folder / ".env", use_environment=False)
            elif command == "logs":
                log = folder / "bot.log"
                content = "\n".join(log.read_text(encoding="utf-8").splitlines()[-40:]) if log.exists() else "尚無日誌"
                values = read_dotenv(folder / ".env")
                for key in ("BOT_TOKEN", "CONTENT_HASH_SECRET"):
                    if values.get(key):
                        content = content.replace(values[key], "[隱藏]")
                print(content)
            elif command == "status":
                state = manager.status(selected)
                print(f"{selected}：{LABELS.get(state['status'], state['status'])}，PID {state['pid'] or '—'}，{state['memory_mb']:.1f} MB")

        if args.command is None:
            dashboard(manager, action)
        elif args.command == "app":
            if _confirm("將停止所有機器人，永久移除全部設定、資料庫、備份及 tgbot 安裝套件。", "DELETE ALL", args.yes):
                from app.uninstall import schedule_uninstall
                log = schedule_uninstall(manager)
                print(f"CLI 結束後會自動解除安裝。結果日誌：{log}")
        elif args.command == "restore":
            name = args.name_or_backup if args.backup_file else None
            backup_file = args.backup_file or Path(args.name_or_backup)
            selected = select_bot(manager, name)
            if selected:
                if manager.process(selected) is not None:
                    raise ConfigurationError("請先停止這台機器人，再執行還原")
                _legacy("restore", manager.folder(selected) / ".env", backup_file.expanduser(), use_environment=False)
        elif getattr(args, "select", False):
            selected = select_bot(manager, None)
            if selected:
                action(args.command, selected)
        else:
            action(args.command, getattr(args, "name", None))
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已離開控制面板；背景機器人維持原狀。")
        return 0
    except ConfigurationError as exc:
        print(f"無法完成：{exc}", file=sys.stderr)
    except TokenValidationError:
        print("Bot Token 格式錯誤，請修改設定。", file=sys.stderr)
    except TelegramAPIError as exc:
        print(f"Telegram 連線失敗（{type(exc).__name__}），請檢查網路、Token 與群組權限。", file=sys.stderr)
    except (RuntimeError, ValueError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
    except sqlite3.Error:
        print("資料庫操作失敗，請確認檔案及備份是否完整。", file=sys.stderr)
    except (OSError, UnicodeError):
        print("檔案或程序操作失敗，請確認路徑、權限及 UTF-8 編碼。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
