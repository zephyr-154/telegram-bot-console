from __future__ import annotations

import asyncio
import sqlite3

import pytest

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


def test_report_reply_upgrade_preserves_reports_and_backs_up_once(tmp_path):
    async def scenario():
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        await db.create_article("XG_ABCDEFGH", 42, "digest")
        await db.create_report("XG_ABCDEFGH", 43, "原有檢舉原因", "2026-10-18T00:00:00+00:00", "2026-10-17T00:00:00+00:00")
        report_id = (await db.list_open_reports(10, 0))[0]["report_id"]
        db.connection.execute("ALTER TABLE reports DROP COLUMN reply_status")
        db.connection.execute("ALTER TABLE reports DROP COLUMN reply_message_id")
        await db.close()
        await db.open()
        report = await db.get_report(report_id)
        assert report["reporter_user_id"] == 43 and report["reason"] == "原有檢舉原因"
        assert report["status"] == "open" and report["reply_status"] == "none" and report["reply_message_id"] is None
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
        await db.close()
        await db.open()
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
        await db.close()
    run(scenario())


def test_report_reply_claim_survives_restart_and_prevents_duplicate_sends(tmp_path):
    async def scenario():
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        await db.create_article("XG_ABCDEFGH", 42, "digest")
        await db.create_report("XG_ABCDEFGH", 43, "檢舉", "2026-10-18T00:00:00+00:00", "2026-10-17T00:00:00+00:00")
        report_id = (await db.list_open_reports(10, 0))[0]["report_id"]
        assert await db.claim_report_reply(report_id, 99)
        assert not await db.claim_report_reply(report_id) and not await db.close_report(report_id)
        await db.close()
        await db.open()
        assert (await db.get_report(report_id))["reply_status"] == "uncertain"
        actions = await db.list_report_actions(report_id, 10, 0)
        assert len(actions) == 1 and actions[0]["admin_user_id"] == 99
        assert actions[0]["status"] == "uncertain" and actions[0]["completed_at_utc"]
        await db.finish_report_reply(report_id, "sent", 123)
        assert (await db.list_report_actions(report_id, 10, 0)) == actions
        assert not await db.claim_report_reply(report_id)
        assert await db.close_report(report_id)
        assert not await db.close_report(report_id) and not await db.claim_report_reply(report_id)
        await db.close()
    run(scenario())


def test_report_history_upgrade_preserves_legacy_cases_and_backs_up_before_migration(tmp_path):
    async def scenario():
        db = Database(tmp_path / "bot.sqlite3")
        await db.open()
        await db.create_article("XG_ABCDEFGH", 42, "digest")
        published = "2026-10-17T01:00:00+00:00"
        await db.mark_published("XG_ABCDEFGH", published)
        await db.create_report("XG_ABCDEFGH", 43, "舊案件", published, published)
        report_id = (await db.list_open_reports(10, 0))[0]["report_id"]
        db.connection.execute("UPDATE reports SET status = 'closed', reply_status = 'sent', reply_message_id = 123")
        db.connection.execute("DROP TABLE report_actions")
        await db.close()
        await db.open()
        report = await db.get_report(report_id)
        assert report["status"] == "closed" and report["reply_status"] == "sent" and report["reply_message_id"] == 123
        assert (await db.get_article("XG_ABCDEFGH"))["published_at_utc"] == published
        assert not await db.list_report_actions(report_id, 10, 0)
        assert [row["report_id"] for row in await db.list_closed_reports(10, 0)] == [report_id]
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        snapshot = sqlite3.connect(backups[0])
        try:
            assert not snapshot.execute("SELECT 1 FROM sqlite_master WHERE name = 'report_actions'").fetchone()
            assert snapshot.execute("SELECT reason FROM reports").fetchone()[0] == "舊案件"
        finally:
            snapshot.close()
        await db.close()
        await db.open()
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
        await db.close()
    run(scenario())


@pytest.mark.parametrize("operation", ["claim", "finish", "close"])
def test_report_state_and_history_commit_together(ctx, operation):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "digest")
        await ctx.db.create_report("XG_ABCDEFGH", 43, "檢舉", "2026-10-18T00:00:00+00:00", "2026-10-17T00:00:00+00:00")
        report_id = (await ctx.db.list_open_reports(10, 0))[0]["report_id"]
        if operation == "finish":
            assert await ctx.db.claim_report_reply(report_id, 99)
        before = await ctx.db.get_report(report_id)
        actions_before = await ctx.db.list_report_actions(report_id, 10, 0)
        event = "UPDATE" if operation == "finish" else "INSERT"
        ctx.db.connection.execute(
            f"CREATE TRIGGER reject_history BEFORE {event} ON report_actions BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
        )

        async def act():
            if operation == "claim":
                return await ctx.db.claim_report_reply(report_id, 99)
            if operation == "finish":
                return await ctx.db.finish_report_reply(report_id, "sent", 123)
            return await ctx.db.close_report(report_id, 100)

        with pytest.raises(sqlite3.IntegrityError, match="history unavailable"):
            await act()
        assert await ctx.db.get_report(report_id) == before
        assert await ctx.db.list_report_actions(report_id, 10, 0) == actions_before
        ctx.db.connection.execute("DROP TRIGGER reject_history")
        await act()
        assert await ctx.db.get_report(report_id) != before
        assert await ctx.db.list_report_actions(report_id, 10, 0) != actions_before
    run(scenario())
