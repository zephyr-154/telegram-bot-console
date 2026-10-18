from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.config import Settings
from app.context import AppContext
from app.database import Database
from app.services.deletion import delete_article_messages
from app.services.rate_limit import check_publish_limits
from app.services.publishing import send_rendered_images
from app.utils import iso_utc


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    def __init__(self):
        self.photo_calls = []
        self.media_calls = []
        self.deleted = []

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        return SimpleNamespace(message_id=11, chat=SimpleNamespace(id=-1009))

    async def send_media_group(self, **kwargs):
        self.media_calls.append(kwargs)
        return [
            SimpleNamespace(message_id=21, chat=SimpleNamespace(id=-1009)),
            SimpleNamespace(message_id=25, chat=SimpleNamespace(id=-1009)),
        ]

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


def _settings(tmp_path) -> Settings:
    font = Path("C:/Windows/Fonts/msjh.ttc")
    return Settings(
        bot_token="token",
        channel_id=-1009,
        admin_user_id=99,
        database_path=tmp_path / "bot.sqlite3",
        temp_dir=tmp_path / "tmp",
        font_regular_path=font,
        font_bold_path=Path("C:/Windows/Fonts/msjhbd.ttc"),
        telegram_logo_path=None,
        content_hash_secret="s" * 32,
    )


def test_single_and_multiple_images_use_different_telegram_methods() -> None:
    async def scenario() -> None:
        bot = FakeBot()
        one = await send_rendered_images(bot, -1009, "XG_ABCDEFGH", [b"one"], "Asia/Taipei")
        many = await send_rendered_images(bot, -1009, "XG_IJKLMNPQ", [b"one", b"two"], "Asia/Taipei")
        assert [message.message_id for message in one] == [11]
        assert [message.message_id for message in many] == [21, 25]
        assert len(bot.photo_calls) == 1
        assert len(bot.media_calls) == 1
        assert bot.photo_calls[0]["caption"].startswith("#XG_ABCDEFGH\n")
        media = bot.media_calls[0]["media"]
        assert media[0].caption.startswith("#XG_IJKLMNPQ\n")
        assert media[1].caption is None

    run(scenario())


def test_daily_limit_resets_at_taipei_midnight(tmp_path, monkeypatch) -> None:
    async def scenario():
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            before = datetime(2026, 10, 16, 15, 59, 59, tzinfo=timezone.utc)
            monkeypatch.setattr("app.services.rate_limit.utc_now", lambda: before)
            monkeypatch.setattr("app.services.rate_limit.taipei_now", lambda zone: before.astimezone(ZoneInfo(zone)))
            for index in range(20):
                article_id = f"XG_{'ABCDEFGHJKLMNPQRSTUVWXYZ'[index]}BCDEFGH"
                await db.create_article(article_id, 42, str(index))
                await db.mark_published(article_id, iso_utc(before - timedelta(hours=1, minutes=index)))
            assert "今日發布額度" in (await check_publish_limits(db, 42, "Asia/Taipei")).message
            after = before + timedelta(seconds=2)
            monkeypatch.setattr("app.services.rate_limit.utc_now", lambda: after)
            monkeypatch.setattr("app.services.rate_limit.taipei_now", lambda zone: after.astimezone(ZoneInfo(zone)))
            assert (await check_publish_limits(db, 42, "Asia/Taipei")).allowed
        finally:
            await db.close()
    run(scenario())


def test_rate_limit_uses_taipei_calendar_day(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            now = datetime.now(timezone.utc)
            # Three recent posts should block regardless of VPS local timezone.
            for index in range(3):
                article_id = f"XG_{'ABCDEFGH'[index]}BCDEFGH"
                await db.create_article(article_id, 42, f"digest-{index}")
                await db.mark_published(article_id, iso_utc(now - timedelta(minutes=index)))
            decision = await check_publish_limits(db, 42, "Asia/Taipei")
            assert not decision.allowed
            assert "10 分鐘" in decision.message
        finally:
            await db.close()

    run(scenario())


def test_failed_channel_delete_does_not_mark_article_deleted(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            await db.save_article_messages("XG_ABCDEFGH", -1009, [7])
            await db.mark_published("XG_ABCDEFGH", iso_utc())
            async def fail_delete(*args, **kwargs):
                return False
            monkeypatch.setattr("app.services.deletion.try_delete_message", fail_delete)
            ctx = AppContext(_settings(tmp_path), db)
            result = await delete_article_messages(ctx, FakeBot(), "XG_ABCDEFGH")
            article = await db.get_article("XG_ABCDEFGH")
            assert not result.success
            assert article["status"] == "published"
            assert article["delete_error_code"] == "telegram_delete_failed"
        finally:
            await db.close()

    run(scenario())


def test_concurrent_delete_requests_are_serialized(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            await db.save_article_messages("XG_ABCDEFGH", -1009, [7, 8])
            await db.mark_published("XG_ABCDEFGH", iso_utc())
            ctx = AppContext(_settings(tmp_path), db)
            results = await asyncio.gather(
                delete_article_messages(ctx, FakeBot(), "XG_ABCDEFGH"),
                delete_article_messages(ctx, FakeBot(), "XG_ABCDEFGH"),
            )
            assert sorted(result.success for result in results) == [False, True]
            article = await db.get_article("XG_ABCDEFGH")
            assert article["status"] == "deleted"
        finally:
            await db.close()

    run(scenario())
