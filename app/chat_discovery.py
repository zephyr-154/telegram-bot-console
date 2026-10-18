from __future__ import annotations

import asyncio
import logging
import unicodedata
from contextlib import ExitStack
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramConflictError, TelegramForbiddenError,
    TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError, validate_token

from app.backup import database_lock
from app.config import ConfigurationError, read_dotenv
from app.manager import BotManager, safe_child
from app.storage import read_json, write_json


CHAT_TYPES = {"group", "supergroup", "channel"}
DISCOVERY_UPDATES = {"my_chat_member", "message", "edited_message", "channel_post", "edited_channel_post"}
CHAT_LABELS = {"group": "群組", "supergroup": "群組", "channel": "頻道"}


def _title(value: str) -> str:
    return "".join(char for char in value if not unicodedata.category(char).startswith("C"))[:160] or "未命名聊天"


class ChatRegistry:
    def __init__(self, folder: Path, token: str):
        try:
            validate_token(token)
            bot_id = int(token.partition(":")[0])
        except (TokenValidationError, ValueError):
            raise ConfigurationError("請先填入 BotFather 提供的完整 Bot Token") from None
        self.folder, self.bot_id = folder, bot_id
        self.path = safe_child(folder, folder / f"chats-{self.bot_id}.json")

    def _entries(self) -> dict[int, dict]:
        state = read_json(safe_child(self.folder, self.path))
        if state.get("bot_id") != self.bot_id or not isinstance(state.get("chats"), list):
            return {}
        result = {}
        for entry in state["chats"]:
            if (not isinstance(entry, dict) or type(entry.get("id")) is not int or entry["id"] >= 0
                    or not isinstance(entry.get("type"), str) or entry["type"] not in CHAT_TYPES
                    or not isinstance(entry.get("title"), str)
                    or not isinstance(entry.get("status", "member"), str)):
                continue
            if type(entry.get("date")) is not int or type(entry.get("update_id")) is not int:
                continue
            result[entry["id"]] = {key: entry[key] for key in ("id", "title", "type", "date", "update_id")}
            result[entry["id"]].update(title=_title(entry["title"]), status=entry.get("status", "member"))
        return result

    def chats(self) -> list[dict]:
        return sorted((entry for entry in self._entries().values() if entry["status"] not in {"left", "kicked"}),
                      key=lambda entry: (entry["title"].casefold(), entry["id"]))

    def remember(self, chat, status: str | None = None, *, date: int | None = None, update_id: int = 0) -> None:
        kind = getattr(chat.type, "value", chat.type)
        if kind not in CHAT_TYPES or chat.id >= 0 or getattr(chat, "is_direct_messages", False):
            return
        safe_child(self.folder, self.path)
        with database_lock(self.path):
            entries = self._entries()
            previous = entries.get(chat.id, {})
            stamp = (date, update_id) if date is not None else (previous.get("date", 0), previous.get("update_id", 0))
            if stamp < (previous.get("date", 0), previous.get("update_id", 0)):
                return
            entry = {"id": chat.id, "title": _title(chat.title or ""), "type": kind,
                     "status": status or previous.get("status", "member"), "date": stamp[0], "update_id": stamp[1]}
            if (any(entry[key] != previous.get(key) for key in ("id", "title", "type", "status"))
                    or status is not None and stamp != (previous.get("date", 0), previous.get("update_id", 0))):
                entries[chat.id] = entry
                write_json(self.path, {"bot_id": self.bot_id, "chats": list(entries.values())})

    def observe(self, update) -> None:
        membership = update.my_chat_member
        if membership is not None:
            member = membership.new_chat_member
            status = getattr(member.status, "value", member.status)
            if status == "restricted" and not member.is_member:
                status = "left"
            self.remember(membership.chat, status, date=int(membership.date.timestamp()), update_id=update.update_id)
        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            message = getattr(update, key)
            if message is not None:
                self.remember(message.chat, "left" if message.migrate_to_chat_id else None,
                              date=int((message.edit_date or message.date).timestamp()), update_id=update.update_id)


def cached_chats(folder: Path, token: str) -> list[dict]:
    try:
        return ChatRegistry(folder, token).chats()
    except (ConfigurationError, OSError):
        return []


class ChatTrackingMiddleware:
    def __init__(self, registry: ChatRegistry):
        self.registry = registry

    async def __call__(self, handler, event, data):
        for attempt in range(3):
            try:
                self.registry.observe(event)
                break
            except (ConfigurationError, OSError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and attempt < 2:
                    await asyncio.sleep(.03)
                    continue
                logging.getLogger(__name__).warning("chat registry update failed error=%s", type(exc).__name__)
                break
        return await handler(event, data)


async def _refresh_known(bot, registry: ChatRegistry, channel_id: str) -> None:
    ids = {entry["id"] for entry in registry.chats()}
    if channel_id.lstrip("-").isdigit() and int(channel_id) < 0:
        ids.add(int(channel_id))
    for chat_id in ids:
        try:
            chat = await bot.get_chat(chat_id)
            member = await bot.get_chat_member(chat_id, registry.bot_id)
            status = member.status
            if status == "restricted" and not member.is_member:
                status = "left"
            registry.remember(chat, status)
        except (TelegramBadRequest, TelegramForbiddenError):
            known = registry._entries().get(chat_id)
            if known:
                from aiogram.types import Chat
                registry.remember(Chat(id=chat_id, title=known["title"], type=known["type"]), "left")


async def watch_chats(manager: BotManager, name: str, token: str, channel_id: str = ""):
    """Listen while the settings panel is open; an active worker owns its update stream."""
    registry = ChatRegistry(manager.folder(name), token)
    try:
        with ExitStack() as locks:
            running = False
            with database_lock(manager.root / "manager"):
                manager.ensure_available()
                for other in manager.names():
                    folder = manager.folder(other)
                    process = manager.process(other)
                    saved_id = read_dotenv(folder / ".env").get("BOT_TOKEN", "").partition(":")[0]
                    state = read_json(folder / "runtime.json")
                    active_id = str(state.get("bot_id", "")) if process is not None else ""
                    if folder != registry.folder and str(registry.bot_id) in {saved_id, active_id}:
                        raise ConfigurationError(f"相同 Bot 已部署在「{other}」，請到該台的設定偵測")
                    if folder == registry.folder and process is not None:
                        if active_id != str(registry.bot_id):
                            raise ConfigurationError("背景程序仍使用另一個 Token，請先停止該台後再偵測")
                        if not state.get("chat_tracking"):
                            raise ConfigurationError("請先重啟這台機器人，讓背景程序載入群組偵測功能")
                        running = True
                if not running:
                    locks.enter_context(database_lock(registry.folder / "worker"))
            async with Bot(token=token, session=AiohttpSession(timeout=20)) as bot:
                me = await bot.get_me()
                username = me.username or "機器人帳號"
                if running:
                    await _refresh_known(bot, registry, channel_id)
                    while manager.process(name) is not None:
                        yield registry.chats(), "背景程序持續偵測中；新增邀請後清單會自動更新"
                        await asyncio.sleep(1)
                    yield registry.chats(), "背景程序已停止；請重新開始偵測"
                    return
                webhook = await bot.get_webhook_info()
                if webhook.url:
                    raise ConfigurationError("此 Bot 正使用 Webhook；請先在原部署停用 Webhook，再開始偵測")
                waiting = f"等待邀請 @{username} 加入群組／頻道；舊群組可傳 /start@{username}，頻道可發新貼文"
                yield registry.chats(), waiting
                first, seen = True, set()
                while True:
                    # shortcut: 不確認更新以保留投稿；滿 100 筆時需啟動正式程序處理，長期獨立偵測再加入可靠轉交。
                    updates = await bot.get_updates(timeout=10, limit=100,
                                                    allowed_updates=sorted(DISCOVERY_UPDATES | {"callback_query"}))
                    for update in updates:
                        if update.update_id not in seen:
                            registry.observe(update)
                    seen = {update.update_id for update in updates}
                    if first:
                        await _refresh_known(bot, registry, channel_id)
                        first = False
                    notice = waiting
                    if len(updates) == 100:
                        notice = "待處理更新已達 100 筆，新通知可能排在後面；請停止偵測並啟動機器人處理，或手動填入 ID"
                    yield registry.chats(), notice
                    await asyncio.sleep(1)
    except TokenValidationError:
        raise ConfigurationError("Bot Token 格式錯誤，請重新複製完整 Token") from None
    except TelegramUnauthorizedError:
        raise ConfigurationError("Token 無效或已被撤銷，請向 BotFather 重新取得") from None
    except TelegramConflictError:
        raise ConfigurationError("其他程式正在接收這台 Bot 的更新；請先停止原部署後再偵測") from None
    except TelegramRetryAfter as exc:
        raise ConfigurationError(f"Telegram 暫時限制請求，請在 {exc.retry_after} 秒後重試") from None
    except (TelegramNetworkError, asyncio.TimeoutError):
        raise ConfigurationError("無法連線到 Telegram，請確認網路後重新開始偵測") from None
    except TelegramAPIError:
        raise ConfigurationError("Telegram 暫時無法取得群組資訊，請稍後重試") from None
    except RuntimeError:
        raise ConfigurationError("其他管理或偵測程序正在使用這台機器人，請結束該操作後重試") from None
