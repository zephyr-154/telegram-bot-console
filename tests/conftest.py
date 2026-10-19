from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.context import AppContext
from app.database import Database
from app.utils import iso_utc, utc_now


class FakeMessage:
    def __init__(self, bot, user_id=42, text="", message_id=1, chat_id=None):
        self.bot = bot
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id if chat_id is None else chat_id, type="private")
        self.message_id = message_id
        self.text = text
        self.date = utc_now()

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(chat_id=self.chat.id, text=text, **kwargs)

    async def edit_text(self, text, **kwargs):
        return await self.bot.edit_message_text(chat_id=self.chat.id, message_id=self.message_id, text=text, **kwargs)


class FakeBot:
    def __init__(self):
        self.serial = 100
        self.messages = []
        self.edits = []
        self.markup_edits = []
        self.photo_calls = []
        self.media_calls = []
        self.deleted = []
        self.pinned = []

    def message(self, user_id=42, text="", message_id=1):
        return FakeMessage(self, user_id, text, message_id)

    async def send_message(self, **kwargs):
        assert len(kwargs["text"]) <= 4096
        self.messages.append(kwargs)
        self.serial += 1
        return FakeMessage(self, 999, kwargs["text"], self.serial, kwargs["chat_id"])

    async def edit_message_text(self, **kwargs):
        assert len(kwargs["text"]) <= 4096
        self.edits.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        self.markup_edits.append(kwargs)

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        self.serial += 1
        return FakeMessage(self, 999, "", self.serial, kwargs["chat_id"])

    async def send_media_group(self, **kwargs):
        self.media_calls.append(kwargs)
        result = []
        for _ in kwargs["media"]:
            self.serial += 2
            result.append(FakeMessage(self, 999, "", self.serial, kwargs["chat_id"]))
        return result

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    async def get_chat(self, chat_id, **kwargs):
        return SimpleNamespace(id=chat_id, title=f"測試群組 {chat_id}")

    async def pin_chat_message(self, **kwargs):
        self.pinned.append(kwargs)
        return True


class FakeCallback:
    def __init__(self, bot, data, user_id=42, message_id=2):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(bot, 999, "", message_id, user_id)
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.sqlite3")
    asyncio.run(db.open())
    settings = Settings(
        bot_token="1234567890:test", channel_id=-1001234, admin_user_id=99,
        database_path=db.path, temp_dir=tmp_path / "tmp",
        font_regular_path=Path("unused-font"), font_bold_path=Path("unused-bold-font"),
        telegram_logo_path=None, content_hash_secret="s" * 32,
    )
    monkeypatch.setattr("app.services.publishing.render_post_images", lambda *args, **kwargs: [b"png"])
    yield AppContext(settings, db)
    asyncio.run(db.close())


@pytest.fixture
def bot():
    return FakeBot()


async def make_pending(ctx, user_id=42, text="這是一篇測試投稿", nonce="draft", created=None):
    await ctx.db.replace_pending(
        user_id=user_id, source_chat_id=user_id, source_message_id=1, raw_text=text,
        callback_nonce=nonce, created_at_utc=created or iso_utc(),
        target_chat_ids=ctx.settings.channel_ids,
    )
    await ctx.db.update_pending_confirmation(user_id, 2)
    return await ctx.db.get_pending_for_user(user_id)
