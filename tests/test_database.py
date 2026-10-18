from __future__ import annotations

import asyncio
import sqlite3

from app.database import Database


def run(coro):
    return asyncio.run(coro)


def test_pending_submission_replaces_previous(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            assert await db.replace_pending(
                user_id=42,
                source_chat_id=42,
                source_message_id=100,
                raw_text="第一篇",
                callback_nonce="old",
                created_at_utc="2026-08-22T00:00:00+00:00",
            ) is None
            old = await db.replace_pending(
                user_id=42,
                source_chat_id=42,
                source_message_id=101,
                raw_text="第二篇",
                callback_nonce="new",
                created_at_utc="2026-08-22T00:01:00+00:00",
            )
            assert old["raw_text"] == "第一篇"
            pending = await db.get_pending_for_user(42)
            assert pending["raw_text"] == "第二篇"
            assert await db.get_pending_by_nonce("old") is None
        finally:
            await db.close()

    run(scenario())


def test_existing_database_is_upgraded_without_losing_articles(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE articles (article_id TEXT PRIMARY KEY, author_user_id INTEGER NOT NULL, "
        "content_hmac TEXT NOT NULL, status TEXT NOT NULL, published_at_utc TEXT, deleted_at_utc TEXT, "
        "delete_failed_at_utc TEXT, delete_error_code TEXT)"
    )
    connection.execute("INSERT INTO articles VALUES ('XG_ABCDEFGH', 42, 'digest', 'published', '2026-08-22T01:00:00+00:00', NULL, NULL, NULL)")
    connection.execute("INSERT INTO articles VALUES ('XG_BCDEFGHJ', 43, 'unknown', 'publishing', NULL, NULL, NULL, NULL)")
    connection.commit()
    connection.close()

    async def scenario():
        db = Database(path)
        await db.open()
        try:
            original = await db.get_article("XG_ABCDEFGH")
            assert original["author_user_id"] == 42 and original["content_hmac"] == "digest"
            assert original["published_at_utc"] == "2026-08-22T01:00:00+00:00"
            assert await db.discard_incomplete_articles() == 0
            uncertain = await db.get_article("XG_BCDEFGHJ")
            assert uncertain["publish_error_code"] == "interrupted_legacy_publish"
            await db.close()
            await db.open()
            assert await db.get_article("XG_ABCDEFGH")
            assert await db.get_article("XG_BCDEFGHJ")
        finally:
            await db.close()
    run(scenario())


def test_duplicate_and_rate_count_include_deleted_articles(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            await db.save_article_messages("XG_ABCDEFGH", -1001, [7, 8])
            await db.mark_published("XG_ABCDEFGH", "2026-08-22T01:00:00+00:00")
            await db.mark_deleted("XG_ABCDEFGH", "2026-08-22T02:00:00+00:00")
            assert await db.has_recent_duplicate(
                42, "digest", "2026-08-21T02:00:00+00:00"
            )
            assert await db.count_user_between(
                42,
                "2026-08-22T00:00:00+00:00",
                "2026-08-23T00:00:00+00:00",
            ) == 1
            assert [row["message_id"] for row in await db.get_article_messages("XG_ABCDEFGH")] == [7, 8]
        finally:
            await db.close()

    run(scenario())


def test_startup_cleanup_removes_publishing_reservations(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            assert await db.discard_incomplete_articles() == 1
            assert await db.get_article("XG_ABCDEFGH") is None
        finally:
            await db.close()

    run(scenario())


def test_startup_cleanup_keeps_reservations_with_channel_messages(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            await db.save_article_messages("XG_ABCDEFGH", -1001, [7])
            assert await db.discard_incomplete_articles() == 0
            article = await db.get_article("XG_ABCDEFGH")
            assert article is not None
            assert article["status"] == "publishing"
        finally:
            await db.close()

    run(scenario())


def test_save_article_messages_is_idempotent_for_publish_retry(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        try:
            await db.create_article("XG_ABCDEFGH", 42, "digest")
            await db.save_article_messages("XG_ABCDEFGH", -1001, [7, 11])
            # Retrying after a Telegram response must not duplicate rows or
            # turn a recoverable database retry into a publish failure.
            await db.save_article_messages("XG_ABCDEFGH", -1001, [7, 11])
            rows = await db.get_article_messages("XG_ABCDEFGH")
            assert [(row["sequence_no"], row["message_id"]) for row in rows] == [
                (1, 7),
                (2, 11),
            ]
        finally:
            await db.close()

    run(scenario())


def test_v04_database_upgrade_backs_up_and_preserves_posts_and_pending_text(tmp_path):
    async def scenario():
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        await db.create_article("XG_ABCDEFGH", 42, "digest")
        await db.save_article_messages("XG_ABCDEFGH", -1001, [7])
        await db.mark_published("XG_ABCDEFGH", "2026-10-18T00:00:00+00:00")
        await db.replace_pending(user_id=84, source_chat_id=84, source_message_id=8, raw_text="待確認原文",
                                callback_nonce="old", created_at_utc="2026-10-18T00:00:00+00:00")
        db.connection.execute("ALTER TABLE articles DROP COLUMN target_chat_ids")
        db.connection.execute("ALTER TABLE pending_submissions DROP COLUMN target_chat_ids")
        await db.close()
        await db.open()
        assert (await db.get_article("XG_ABCDEFGH"))["target_chat_ids"] is None
        assert (await db.get_article_messages("XG_ABCDEFGH"))[0]["message_id"] == 7
        assert (await db.get_pending_for_user(84))["raw_text"] == "待確認原文"
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        await db.close()
        await db.open()
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
        await db.close()
    run(scenario())
