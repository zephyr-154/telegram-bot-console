import asyncio
from dataclasses import replace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.handlers.admin import admin_callback
from app.handlers.private import admin_command
from app.services.admin_badges import refresh_admin_badges
from app.utils import iso_utc
from conftest import FakeCallback, make_pending


ARTICLE = "XG_ABCDEFGH"


def labels(keyboard):
    return {button.callback_data: button.text for row in keyboard.inline_keyboard for button in row}


async def seed(ctx, count=1):
    await ctx.db.create_article(ARTICLE, 43, "hash")
    await ctx.db.mark_published(ARTICLE, iso_utc())
    for user_id in range(43, 43 + count):
        await ctx.db.create_report(ARTICLE, user_id, "原因", iso_utc(), iso_utc())


def test_badges_count_all_open_reports_and_unique_problem_articles(ctx, bot):
    async def scenario():
        await seed(ctx, count=4)
        await ctx.db.mark_delete_failed(ARTICLE, iso_utc(), "telegram_delete_failed")
        await make_pending(ctx)
        assert await ctx.db.attention_counts() == (1, 4)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        assert labels(bot.messages[-1]["reply_markup"])["admin:pending"] == "待處理事項 🔴 5"
        state = await ctx.db.get_state(99)
        panel_id = state["payload"]["panel_message_id"]
        await admin_callback(FakeCallback(bot, "admin:pending", user_id=99, message_id=panel_id), bot, ctx)
        buttons = labels(bot.edits[-1]["reply_markup"])
        assert buttons["admin:issues:0"] == "發布／刪文待處理 🔴 1"
        assert buttons["admin:reports:0"] == "檢舉待處理 🔴 4"
        assert buttons["admin:back"] == "返回管理面板"
        for row in await ctx.db.list_open_reports(10, 0):
            await ctx.db.close_report(row["report_id"])
        await ctx.db.mark_deleted(ARTICLE, iso_utc())
        await refresh_admin_badges(ctx, bot)
        assert all("🔴" not in text for text in labels(bot.markup_edits[-1]["reply_markup"]).values())
        assert await ctx.db.attention_counts() == (0, 0)
    asyncio.run(scenario())


def test_report_list_badges_point_to_reports_and_back_only_to_other_category(ctx, bot):
    async def scenario():
        await seed(ctx)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        await admin_callback(FakeCallback(bot, "admin:reports:0", user_id=99, message_id=panel_id), bot, ctx)
        report_id = (await ctx.db.list_open_reports(10, 0))[0]["report_id"]
        buttons = labels(bot.edits[-1]["reply_markup"])
        assert buttons[f"admin:report:{report_id}"] == f"處理檢舉 #{report_id} 🔴 1"
        assert buttons["admin:pending"] == "返回待處理事項"
        await ctx.db.mark_delete_failed(ARTICLE, iso_utc(), "telegram_delete_failed")
        await refresh_admin_badges(ctx, bot)
        buttons = labels(bot.markup_edits[-1]["reply_markup"])
        assert buttons["admin:pending"] == "返回待處理事項 🔴 1"
        assert buttons[f"admin:report:{report_id}"].endswith("🔴 1")
        await ctx.db.mark_deleted(ARTICLE, iso_utc())
        await refresh_admin_badges(ctx, bot)
        assert labels(bot.markup_edits[-1]["reply_markup"])["admin:pending"] == "返回待處理事項"
        await ctx.db.close_report(report_id)
        await refresh_admin_badges(ctx, bot)
        assert not any("🔴" in text for text in labels(bot.markup_edits[-1]["reply_markup"]).values())
    asyncio.run(scenario())


def test_issue_list_back_badge_only_counts_reports(ctx, bot):
    async def scenario():
        await seed(ctx, count=2)
        await ctx.db.mark_delete_failed(ARTICLE, iso_utc(), "telegram_delete_failed")
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        await admin_callback(FakeCallback(bot, "admin:issues:0", user_id=99, message_id=panel_id), bot, ctx)
        buttons = labels(bot.edits[-1]["reply_markup"])
        assert buttons[f"admin:review:{ARTICLE}"] == f"處理 #{ARTICLE} 🔴 1"
        assert buttons["admin:pending"] == "返回待處理事項 🔴 2"
    asyncio.run(scenario())


def test_current_report_is_excluded_from_return_to_report_list_badge(ctx, bot):
    async def scenario():
        await seed(ctx)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        report_id = (await ctx.db.list_open_reports(10, 0))[0]["report_id"]
        await admin_callback(FakeCallback(bot, f"admin:report:{report_id}", user_id=99, message_id=panel_id), bot, ctx)
        assert labels(bot.edits[-1]["reply_markup"])["admin:reports:0"] == "返回檢舉清單"
        await ctx.db.create_report(ARTICLE, 44, "另外一件", iso_utc(), iso_utc())
        await refresh_admin_badges(ctx, bot)
        assert labels(bot.markup_edits[-1]["reply_markup"])["admin:reports:0"] == "返回檢舉清單 🔴 1"
    asyncio.run(scenario())


def test_report_confirmation_return_does_not_badge_current_report(ctx, bot):
    async def scenario():
        await seed(ctx)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        report_id = (await ctx.db.list_open_reports(10, 0))[0]["report_id"]
        await admin_callback(FakeCallback(bot, f"admin:report_close:{report_id}", user_id=99, message_id=panel_id), bot, ctx)
        assert labels(bot.edits[-1]["reply_markup"])[f"admin:report:{report_id}"] == "返回檢舉／先回覆"
    asyncio.run(scenario())


def test_pagination_does_not_duplicate_badge_for_current_category(ctx, bot):
    async def scenario():
        await seed(ctx, count=4)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        await admin_callback(FakeCallback(bot, "admin:reports:1", user_id=99, message_id=panel_id), bot, ctx)
        buttons = labels(bot.edits[-1]["reply_markup"])
        assert buttons["admin:reports:0"] == "上一頁"
        assert buttons["admin:pending"] == "返回待處理事項"
    asyncio.run(scenario())


def test_visible_panels_refresh_for_all_admins_without_resetting_state(ctx, bot):
    async def scenario():
        ctx.settings = replace(ctx.settings, additional_admin_user_ids=(100,))
        for user_id in (99, 100):
            await admin_command(bot.message(user_id=user_id, text="/admin"), bot, ctx)
        before = {user_id: await ctx.db.get_state(user_id) for user_id in (99, 100)}
        await seed(ctx)
        await refresh_admin_badges(ctx, bot)
        assert {call["chat_id"] for call in bot.markup_edits} == {99, 100}
        for call in bot.markup_edits:
            assert labels(call["reply_markup"])["admin:pending"] == "待處理事項 🔴 1"
        for user_id in (99, 100):
            assert await ctx.db.get_state(user_id) == before[user_id]
        await refresh_admin_badges(ctx, bot)
        assert len(bot.markup_edits) == 2
        report = (await ctx.db.list_open_reports(10, 0))[0]
        await ctx.db.close_report(report["report_id"])
        await refresh_admin_badges(ctx, bot)
        assert len(bot.markup_edits) == 4
        assert labels(bot.markup_edits[-1]["reply_markup"])["admin:pending"] == "待處理事項"
    asyncio.run(scenario())


def test_refresh_skips_busy_admin_and_catches_up_after_operation(ctx, bot):
    async def scenario():
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await seed(ctx)
        async with ctx.user_locks[99]:
            await refresh_admin_badges(ctx, bot)
        assert not bot.markup_edits
        await refresh_admin_badges(ctx, bot)
        assert len(bot.markup_edits) == 1
        await ctx.db.clear_state(99)
        await refresh_admin_badges(ctx, bot)
        assert not ctx.admin_badge_cache
    asyncio.run(scenario())


def test_confirmation_buttons_and_nonce_survive_badge_refresh(ctx, bot):
    async def scenario():
        from app.handlers.admin import update_admin_panel
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="確認", callback_data="admin:force_yes:XG_ABCDEFGH:secret-nonce")],
            [InlineKeyboardButton(text="取消", callback_data="admin:back")],
        ])
        await update_admin_panel(bot, ctx, user_id=99, chat_id=99, current_panel_id=None,
                                 text="確定？", reply_markup=keyboard, state_name="admin_force_confirm",
                                 state_payload={"article_id": ARTICLE, "nonce": "secret-nonce"})
        state = await ctx.db.get_state(99)
        await seed(ctx)
        await refresh_admin_badges(ctx, bot)
        buttons = labels(bot.markup_edits[-1]["reply_markup"])
        assert buttons["admin:force_yes:XG_ABCDEFGH:secret-nonce"] == "確認"
        assert buttons["admin:back"] == "取消 🔴 1"
        assert await ctx.db.get_state(99) == state
    asyncio.run(scenario())
