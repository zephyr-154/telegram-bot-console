from __future__ import annotations

import getpass
import os
import secrets
import sys
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.token import TokenValidationError, validate_token

from app.config import ConfigurationError, read_dotenv, settings_from_values
from app.storage import atomic_write as _atomic_write


FIELDS = {
    "BOT_TOKEN": "Bot Token",
    "CHANNEL_ID": "目標群組／頻道 ID",
    "ADMIN_USER_ID": "管理員 User ID",
    "FONT_REGULAR_PATH": "一般字型路徑",
    "FONT_BOLD_PATH": "粗體字型路徑",
    "TIMEZONE": "時區",
    "TELEGRAM_LOGO_PATH": "Logo 路徑（選填）",
}
DEFAULTS = {
    "BOT_TOKEN": "", "CHANNEL_ID": "", "ADMIN_USER_ID": "",
    "DATABASE_PATH": "./data/bot.sqlite3", "TEMP_DIR": "./data/tmp",
    "FONT_REGULAR_PATH": "./assets/fonts/NotoSansTC-Regular.otf",
    "FONT_BOLD_PATH": "./assets/fonts/NotoSansTC-Bold.otf",
    "TELEGRAM_LOGO_PATH": "", "CONTENT_HASH_SECRET": "", "TIMEZONE": "Asia/Taipei",
}


def initial_values(path: Path) -> dict[str, str]:
    values = DEFAULTS | read_dotenv(path)
    if not path.exists() or values["CONTENT_HASH_SECRET"].startswith("replace_with_") or values["CONTENT_HASH_SECRET"] == "請改成至少32字元的隨機秘密":
        values["CONTENT_HASH_SECRET"] = secrets.token_hex(32)
    for key, filename in (("FONT_REGULAR_PATH", "msjh.ttc"), ("FONT_BOLD_PATH", "msjhbd.ttc")):
        if values[key] != DEFAULTS[key]:
            continue
        candidates = [path.parent / DEFAULTS[key]]
        suffix = "Regular" if key == "FONT_REGULAR_PATH" else "Bold"
        if sys.platform == "darwin":
            candidates.extend(directory / f"NotoSansCJKtc-{suffix}.otf" for directory in (
                Path.home() / "Library" / "Fonts", Path("/Library/Fonts"),
            ))
        elif os.name == "nt":
            candidates.append(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename)
        else:
            candidates.append(Path(f"/usr/share/fonts/opentype/noto/NotoSansCJK-{suffix}.ttc"))
        values[key] = str(next((p for p in candidates if p.is_file()), candidates[0]))
    return values


def validate_field(key: str, value: str, base_dir: Path) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError(f"{key} 不可包含換行或控制字元")
    if key == "BOT_TOKEN":
        try:
            validate_token(value)
        except TokenValidationError:
            raise ConfigurationError("Bot Token 格式錯誤，請複製 BotFather 提供的完整 Token") from None
        if value.startswith("123456789:") or "replace_me" in value:
            raise ConfigurationError("請填入真實 Bot Token")
    elif key in {"CHANNEL_ID", "ADMIN_USER_ID"}:
        try:
            number = int(value)
        except ValueError:
            raise ConfigurationError(f"{key} 必須是整數") from None
        if (key == "CHANNEL_ID" and number >= 0) or (key == "ADMIN_USER_ID" and number <= 0):
            raise ConfigurationError("群組／頻道 ID 必須是負整數；管理員 ID 必須是正整數")
    elif key == "TIMEZONE":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError("時區無效，例如 Asia/Taipei") from None
    elif key in {"FONT_REGULAR_PATH", "FONT_BOLD_PATH", "TELEGRAM_LOGO_PATH"}:
        if key == "TELEGRAM_LOGO_PATH" and not value:
            return
        if not value or not (base_dir / value).is_file():
            raise ConfigurationError(f"{FIELDS[key]}找不到檔案")


def validate_values(values: dict[str, str], base_dir: Path) -> None:
    for key in FIELDS:
        validate_field(key, values.get(key, ""), base_dir)
    settings_from_values(values, base_dir).validate_runtime()


def render_env(original: bytes | None, values: dict[str, str]) -> bytes:
    lines = []
    seen: set[str] = set()
    for line in (original or b"").decode("utf-8-sig").splitlines():
        key = line.partition("=")[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in DEFAULTS:
            if key not in seen:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key}={values[key]}" for key in DEFAULTS if key not in seen)
    return ("\n".join(lines) + "\n").encode("utf-8")


def save_config(path: Path, values: dict[str, str], original: bytes | None) -> bool:
    validate_values(values, path.parent)
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise ConfigurationError("設定檔已被其他程式修改，請離開並重新開啟選單")
    content = render_env(original, values)
    if content == original:
        return False
    if original is not None:
        _atomic_write(path.with_name(path.name + ".bak"), original)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, content)
    return True


def edit_field(values: dict[str, str], key: str, base_dir: Path) -> None:
    prompt = FIELDS[key] + "（Enter 保留" + ("，輸入 - 清除" if key == "TELEGRAM_LOGO_PATH" else "") + "）："
    if key == "BOT_TOKEN":
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(prompt)
            except getpass.GetPassWarning:
                raise ConfigurationError("目前終端無法隱藏輸入，請在互動式 CMD／終端機重試") from None
    else:
        value = input(prompt)
    value = value.strip()
    if not value:
        return
    if key.endswith("_PATH") and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if key == "TELEGRAM_LOGO_PATH" and value == "-":
        value = ""
    validate_field(key, value, base_dir)
    values[key] = value


def interactive_menu(path: Path, *, validator=None, saver=None) -> int:
    original = path.read_bytes() if path.exists() else None
    values = initial_values(path)
    clean_values = values.copy()
    print("\nTelegram 匿名投稿機器人設定")
    print(f"設定檔：{path}")
    if original is None:
        print("首次設定：先填寫 1～3；字型會自動尋找，秘密金鑰會自動產生。")
    print("修改後選 8 儲存；Ctrl+C 可離開，未儲存的修改會放棄。")
    print("啟動機器人：tgbot start；檢查：tgbot check；備份：tgbot backup。")
    while True:
        print("\n目前設定：")
        for key, label in FIELDS.items():
            value = values[key]
            display = "已設定（隱藏）" if key == "BOT_TOKEN" and value else value or "未設定"
            print(f"  {label}：{display}")
        print("\n1 Token  2 群組／頻道 ID  3 管理員 ID  4 字型")
        print("5 時區  6 Logo  7 檢查設定  8 儲存並離開  0 離開")
        choice = input("請選擇：").strip()
        try:
            keys = {"1": "BOT_TOKEN", "2": "CHANNEL_ID", "3": "ADMIN_USER_ID", "5": "TIMEZONE", "6": "TELEGRAM_LOGO_PATH"}
            if choice in keys:
                edit_field(values, keys[choice], path.parent)
            elif choice == "4":
                edit_field(values, "FONT_REGULAR_PATH", path.parent)
                edit_field(values, "FONT_BOLD_PATH", path.parent)
            elif choice == "7":
                validate_values(values, path.parent)
                print("設定格式及檔案檢查通過；啟動 Bot 時才會連線驗證 Token 與群組權限。")
            elif choice == "8":
                if validator is not None:
                    validator(values)
                saved = saver(values, original) if saver is not None else save_config(path, values, original)
                print("設定已儲存。" if saved else "設定沒有變更。")
                if saved and original is not None:
                    print(f"上次設定備份：{path.name}.bak（含秘密，請妥善保存）")
                print("請重新啟動 Bot 讓設定生效。")
                return 0
            elif choice == "0":
                if values != clean_values and input("放棄尚未儲存的修改？[y/N] ").strip().lower() != "y":
                    continue
                return 0
            else:
                print("請輸入 0～8 的選項。")
        except (ConfigurationError, OSError, UnicodeError) as exc:
            print(f"操作未完成：{exc}" if isinstance(exc, ConfigurationError) else "檔案讀寫失敗，請確認路徑、權限及 UTF-8 編碼。")

