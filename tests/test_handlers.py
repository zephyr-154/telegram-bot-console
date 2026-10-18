from __future__ import annotations

import asyncio
from datetime import timedelta
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app.handlers.admin import admin_callback
from app.handlers.private import deletion_callback, private_text, submission_callback
from app.handlers.user import cancel_command, my_posts_callback, my_posts_command, report_command
from app.main import _cleanup_stale_data
from app.utils import iso_utc, utc_now
from conftest import FakeCallback, make_pending


def test_preview_trims_edges_without_losing_internal_newlines(ctx, bot):
    async def scenario():
        text = " " * 100 + "字" * 3996
        await private_text(bot.message(text=text), bot, ctx)
        pending = await ctx.db.get_pending_for_user(42)
        assert pending["raw_text"] == "字" * 3996
        assert len(bot.messages[-1]["text"]) <= 4096
        await private_text(bot.message(text="  第一行\n第二行  ", message_id=3), bot, ctx)
        assert (await ctx.db.get_pending_for_user(42))["raw_text"] == "第一行\n第二行"
    asyncio.run(scenario())


def test_platform_preview_length_rejection_keeps_confirmable_draft(ctx, bot, monkeypatch):
    original = bot.send_message
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TelegramBadRequest(method=SendMessage(chat_id=42, text="unused"), message="message is too long")
        return await original(**kwargs)
    monkeypatch.setattr(bot, "send_message", send)
    async def scenario():
        await private_text(bot.message(text="字" * 4000), bot, ctx)
        pending = await ctx.db.get_pending_for_user(42)
        assert pending and pending["confirmation_message_id"]
        assert "確認你上方的原文" in calls[-1]["text"]
        assert calls[-1]["reply_markup"].inline_keyboard[0][0].callback_data.endswith(pending["callback_nonce"])
    asyncio.run(scenario())


def test_private_admin_command_stays_silent_even_while_busy(ctx, bot):
    async def scenario():
        async with ctx.user_locks[42]:
            await private_text(bot.message(text=" /ADMIN "), bot, ctx)
        assert not bot.messages and not await ctx.db.get_pending_for_user(42)
    asyncio.run(scenario())


def test_submission_callback_rejects_foreign_user_and_old_draft(ctx, bot):
    async def scenario():
        await make_pending(ctx)
        attacker = FakeCallback(bot, "submit:yes:draft", user_id=43)
        await submission_callback(attacker, bot, ctx)
        assert "不屬於你" in attacker.answers[-1][0]
        assert not bot.photo_calls and await ctx.db.get_pending_for_user(42)
        await make_pending(ctx, created=iso_utc(utc_now() - timedelta(hours=25)))
        await submission_callback(FakeCallback(bot, "submit:yes:draft"), bot, ctx)
        assert not bot.photo_calls and not await ctx.db.get_pending_for_user(42)
        assert "24 小時" in bot.messages[-1]["text"]
    asyncio.run(scenario())


def test_double_confirmation_publishes_once_and_removes_raw_draft(ctx, bot):
    async def scenario():
        await make_pending(ctx)
        await asyncio.gather(*[
            submission_callback(FakeCallback(bot, "submit:yes:draft"), bot, ctx) for _ in range(2)
        ])
        assert len(bot.photo_calls) == 1
        assert not await ctx.db.get_pending_for_user(42)
        articles = await ctx.db.list_user_articles(42, 10, 0)
        assert len(articles) == 1
        assert bot.photo_calls[0]["caption"].splitlines()[0] == "#" + articles[0]["article_id"]
    asyncio.run(scenario())


def test_cancel_cleans_pending_and_delete_state(ctx, bot):
    async def scenario():
        await make_pending(ctx)
        await ctx.db.set_state(42, "delete_wait_id", {"prompt_message_id": 7}, iso_utc())
        await cancel_command(bot.message(text="/cancel", message_id=8), bot, ctx)
        assert not await ctx.db.get_pending_for_user(42)
        assert not await ctx.db.get_state(42)
        assert {1, 2, 7} <= {row["message_id"] for row in bot.deleted}
    asyncio.run(scenario())


def test_periodic_cleanup_removes_only_idle_expired_drafts(ctx, bot):
    async def scenario():
        old = iso_utc(utc_now() - timedelta(hours=25))
        await make_pending(ctx, created=old)
        await make_pending(ctx, user_id=43, nonce="active", created=old)
        await make_pending(ctx, user_id=44, nonce="fresh")
        async with ctx.user_locks[43]:
            await _cleanup_stale_data(ctx, bot)
        assert not await ctx.db.get_pending_for_user(42)
        assert await ctx.db.get_pending_for_user(43)
        assert await ctx.db.get_pending_for_user(44)
        await _cleanup_stale_data(ctx, bot)
        assert not await ctx.db.get_pending_for_user(43)
    asyncio.run(scenario())


def test_my_posts_hides_other_authors_and_uses_confirmed_deletion(ctx, bot):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "one")
        await ctx.db.save_article_messages("XG_ABCDEFGH", -1001234, [8, 12])
        await ctx.db.mark_published("XG_ABCDEFGH", iso_utc())
        await ctx.db.create_article("XG_BCDEFGHJ", 43, "two")
        await ctx.db.mark_published("XG_BCDEFGHJ", iso_utc())
        await my_posts_command(bot.message(text="/myposts"), ctx)
        assert "XG_ABCDEFGH" in bot.messages[-1]["text"]
        assert "XG_BCDEFGHJ" not in bot.messages[-1]["text"]
        attacker = FakeCallback(bot, "mine:delete:XG_ABCDEFGH", user_id=43)
        await my_posts_callback(attacker, bot, ctx)
        assert not await ctx.db.get_state(43) and not bot.deleted
        await my_posts_callback(FakeCallback(bot, "mine:open:XG_ABCDEFGH"), bot, ctx)
        keyboard = bot.edits[-1]["reply_markup"].inline_keyboard
        assert keyboard[0][0].url == "https://t.me/c/1234/8"
        await my_posts_callback(FakeCallback(bot, "mine:delete:XG_ABCDEFGH"), bot, ctx)
        state = await ctx.db.get_state(42)
        assert state["state"] == "delete_confirm" and not bot.deleted
        payload = state["payload"]
        await deletion_callback(FakeCallback(bot, f"delete:yes:XG_ABCDEFGH:{payload['nonce']}", message_id=payload["confirmation_message_id"]), bot, ctx)
        assert (await ctx.db.get_article("XG_ABCDEFGH"))["status"] == "deleted"
        assert {8, 12} <= {row["message_id"] for row in bot.deleted}
    asyncio.run(scenario())


def test_reports_are_limited_and_notify_without_disclosing_reason(ctx, bot):
    async def scenario():
        ids = ["XG_ABCDEFGH", "XG_BCDEFGHJ", "XG_CDEFGHJK", "XG_DEFGHJKL"]
        for article_id in ids:
            await ctx.db.create_article(article_id, 43, article_id)
            await ctx.db.mark_published(article_id, iso_utc())
        for article_id in ids:
            await report_command(bot.message(text=f"/report {article_id} 測試檢舉原因"), bot, ctx)
        assert len(await ctx.db.list_open_reports(10, 0)) == 3
        assert "太頻繁" in bot.messages[-1]["text"]
        notifications = [row for row in bot.messages if row["chat_id"] == 99]
        assert len(notifications) == 3
        assert all("測試檢舉原因" not in row["text"] for row in notifications)
        await report_command(bot.message(text=f"/report {ids[0]} 重複原因"), bot, ctx)
        assert "已檢舉過" in bot.messages[-1]["text"]
    asyncio.run(scenario())


async def admin_panel(ctx):
    await ctx.db.set_state(99, "admin_panel", {"panel_message_id": 2}, iso_utc())


def test_admin_pause_is_authorized_persistent_and_keeps_delete_available(ctx, bot):
    async def scenario():
        await admin_panel(ctx)
        await admin_callback(FakeCallback(bot, "admin:set_pause:1", user_id=42), bot, ctx)
        assert not await ctx.db.submissions_paused()
        await admin_callback(FakeCallback(bot, "admin:set_pause:1", user_id=99), bot, ctx)
        await admin_callback(FakeCallback(bot, "admin:set_pause:1", user_id=99), bot, ctx)
        assert await ctx.db.submissions_paused()
        await ctx.db.close()
        await ctx.db.open()
        assert await ctx.db.submissions_paused()
        await private_text(bot.message(text="新投稿"), bot, ctx)
        assert "暫停" in bot.messages[-1]["text"]
        assert not await ctx.db.get_pending_for_user(42)
        await private_text(bot.message(text="delete"), bot, ctx)
        assert (await ctx.db.get_state(42))["state"] == "delete_wait_id"
    asyncio.run(scenario())


def test_blocked_list_paginates_beyond_twenty(ctx, bot):
    async def scenario():
        for user_id in range(100, 125):
            await ctx.db.block_user(user_id, iso_utc(), "test")
        await admin_panel(ctx)
        await admin_callback(FakeCallback(bot, "admin:list", user_id=99), bot, ctx)
        first = bot.edits[-1]
        assert any(button.callback_data == "admin:list:1" for row in first["reply_markup"].inline_keyboard for button in row)
        await admin_callback(FakeCallback(bot, "admin:list:1", user_id=99), bot, ctx)
        second = bot.edits[-1]
        assert "第 2 頁" in second["text"]
        assert "100（" in second["text"] and "100（" not in first["text"]
    asyncio.run(scenario())


def test_manual_resolution_requires_current_nonce_and_admin_confirmation(ctx, bot):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "digest")
        await ctx.db.mark_send_started("XG_ABCDEFGH")
        await ctx.db.mark_publish_uncertain("XG_ABCDEFGH", "unknown_result")
        await admin_panel(ctx)
        await admin_callback(FakeCallback(bot, "admin:resolve:XG_ABCDEFGH", user_id=99), bot, ctx)
        state = await ctx.db.get_state(99)
        await admin_callback(FakeCallback(bot, "admin:resolve_yes:XG_ABCDEFGH:wrong", user_id=99), bot, ctx)
        assert (await ctx.db.get_article("XG_ABCDEFGH"))["status"] == "publishing"
        nonce = state["payload"]["nonce"]
        await admin_callback(FakeCallback(bot, f"admin:resolve_yes:XG_ABCDEFGH:{nonce}", user_id=99), bot, ctx)
        assert (await ctx.db.get_article("XG_ABCDEFGH"))["status"] == "deleted"
        assert not await ctx.db.has_recent_duplicate(42, "digest", iso_utc(utc_now() - timedelta(hours=24)))
    asyncio.run(scenario())


def test_admin_issue_and_report_queues_close_reports_separately(ctx, bot):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "digest")
        await ctx.db.mark_publish_uncertain("XG_ABCDEFGH", "unknown_result")
        await ctx.db.create_report("XG_ABCDEFGH", 43, "檢舉原因", iso_utc(), iso_utc(utc_now() - timedelta(minutes=10)))
        await admin_panel(ctx)
        for action in ("pending", "issues:0", "reports:0", "review:XG_ABCDEFGH"):
            await admin_callback(FakeCallback(bot, "admin:" + action, user_id=99), bot, ctx)
        assert "42" in bot.edits[-1]["text"]
        await admin_callback(FakeCallback(bot, "admin:close_reports:XG_ABCDEFGH", user_id=99), bot, ctx)
        assert not await ctx.db.list_open_reports(10, 0)
        assert len(await ctx.db.list_attention_articles(10, 0)) == 1
    asyncio.run(scenario())


def test_admin_backup_button_creates_local_snapshot(ctx, bot):
    async def scenario():
        await admin_panel(ctx)
        await admin_callback(FakeCallback(bot, "admin:backup", user_id=99), bot, ctx)
        assert "備份完成" in bot.edits[-1]["text"]
        assert len(list((ctx.db.path.parent / "backups").glob("bot-backup-*.sqlite3"))) == 1
    asyncio.run(scenario())
