import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.backup import backup_database, restore_database
from app.services.deletion import delete_article_messages
from app.utils import article_status_text, iso_utc, pending_expired


ARTICLE = "XG_ABCDEFGH"
PUBLISHED = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("restore", [False, True], ids=["restart", "restore"])
@pytest.mark.parametrize("elapsed,expired", [
    (timedelta(hours=48, seconds=-1), False),
    (timedelta(hours=48), True),
])
def test_deletion_deadline_uses_original_publication_time_after_downtime(ctx, bot, monkeypatch, restore, elapsed, expired):
    async def scenario():
        await ctx.db.create_article(ARTICLE, 42, "digest")
        await ctx.db.mark_published(ARTICLE, iso_utc(PUBLISHED))
        await ctx.db.save_article_messages(ARTICLE, ctx.settings.channel_id, [7])
        path = ctx.db.path
        snapshot = backup_database(path, path.parent / "backups") if restore else None
        await ctx.db.close()
        now = PUBLISHED + elapsed
        monkeypatch.setattr("app.utils.utc_now", lambda: now)
        monkeypatch.setattr("app.services.deletion.utc_now", lambda: now)
        if restore:
            restore_database(snapshot, path)
        await ctx.db.open()
        await ctx.db.discard_incomplete_articles()
        article = await ctx.db.get_article(ARTICLE)
        assert article["published_at_utc"] == iso_utc(PUBLISHED)
        text = article_status_text(article, ctx.settings.timezone)
        assert ("已超過" in text) == expired
        if not expired:
            assert "10/21 12:00 截止" in text
        result = await delete_article_messages(ctx, bot, ARTICLE)
        assert result.expired == expired and result.success == (not expired)
        assert bool(bot.deleted) == (not expired)
    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed,expired", [
    (timedelta(hours=24, seconds=-1), False),
    (timedelta(hours=24), True),
])
def test_draft_confirmation_deadline_continues_during_downtime(ctx, monkeypatch, elapsed, expired):
    async def scenario():
        await ctx.db.replace_pending(
            user_id=42, source_chat_id=42, source_message_id=1, raw_text="待確認稿件",
            callback_nonce="draft", created_at_utc=iso_utc(PUBLISHED),
            target_chat_ids=ctx.settings.channel_ids,
        )
        path = ctx.db.path
        await ctx.db.close()
        monkeypatch.setattr("app.utils.utc_now", lambda: PUBLISHED + elapsed)
        await ctx.db.open()
        pending = await ctx.db.get_pending_for_user(42)
        assert pending["created_at_utc"] == iso_utc(PUBLISHED)
        assert pending_expired(pending) == expired
    asyncio.run(scenario())
