from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    pass


def default_env_path() -> Path:
    override = os.environ.get("TGBOT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    project_dir = Path(__file__).resolve().parent.parent
    if (project_dir / "tgbot.cmd").is_file():
        return project_dir / ".env"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "TelegramAnonymousBoard" / ".env"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "tgbot" / ".env"


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.setdefault(key.strip(), value)
    return values


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少必要設定：{name}")
    return value


def _int_setting(values: Mapping[str, str], name: str) -> int:
    raw = _required(values, name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"設定 {name} 必須是整數") from exc


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    channel_id: int
    admin_user_id: int
    database_path: Path
    temp_dir: Path
    font_regular_path: Path
    font_bold_path: Path
    telegram_logo_path: Path | None
    content_hash_secret: str
    timezone: str = "Asia/Taipei"

    def validate_runtime(self) -> None:
        if self.bot_token.startswith("123456789:") or "replace_me" in self.bot_token:
            raise ConfigurationError("BOT_TOKEN 尚未填入真實 Bot Token")
        if self.channel_id == 0:
            raise ConfigurationError("CHANNEL_ID 不可為 0")
        if self.admin_user_id <= 0:
            raise ConfigurationError("ADMIN_USER_ID 必須是正整數")
        if len(self.content_hash_secret) < 32 or "replace_with" in self.content_hash_secret:
            raise ConfigurationError("CONTENT_HASH_SECRET 至少需要 32 個字元的隨機秘密")
        if not self.font_regular_path.is_file():
            raise ConfigurationError(f"找不到繁中字型：{self.font_regular_path}")
        if not self.font_bold_path.is_file():
            raise ConfigurationError(f"找不到繁中粗體字型：{self.font_bold_path}")
        if self.telegram_logo_path is not None and not self.telegram_logo_path.is_file():
            raise ConfigurationError(f"找不到 Telegram Logo：{self.telegram_logo_path}")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"找不到時區設定：{self.timezone}") from exc

    def prepare_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def settings_from_values(values: Mapping[str, str], base_dir: Path) -> Settings:
    logo_raw = values.get("TELEGRAM_LOGO_PATH", "").strip()
    return Settings(
        bot_token=_required(values, "BOT_TOKEN"),
        channel_id=_int_setting(values, "CHANNEL_ID"),
        admin_user_id=_int_setting(values, "ADMIN_USER_ID"),
        database_path=_resolve_path(values.get("DATABASE_PATH", "data/bot.sqlite3"), base_dir),
        temp_dir=_resolve_path(values.get("TEMP_DIR", "data/tmp"), base_dir),
        font_regular_path=_resolve_path(_required(values, "FONT_REGULAR_PATH"), base_dir),
        font_bold_path=_resolve_path(_required(values, "FONT_BOLD_PATH"), base_dir),
        telegram_logo_path=_resolve_path(logo_raw, base_dir) if logo_raw else None,
        content_hash_secret=_required(values, "CONTENT_HASH_SECRET"),
        timezone=values.get("TIMEZONE", "Asia/Taipei").strip() or "Asia/Taipei",
    )


def load_settings(env_path: str | Path | None = None, *, use_environment: bool = True) -> Settings:
    env_file = Path(env_path).expanduser().resolve() if env_path is not None else default_env_path()
    values = read_dotenv(env_file)
    if use_environment:
        values.update(os.environ)
    return settings_from_values(values, env_file.parent)
