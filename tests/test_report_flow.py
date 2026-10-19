import asyncio

import pytest

from app.handlers.private import private_text, start_command
from app.handlers.user import cancel_command, report_command
from app.utils import iso_utc
from conftest import make_pending


ARTICLE = "XG_ABCDEFGH"


async def seed(ctx):
    await ctx.db.create_article(ARTICLE, 43, "hash")
    await ctx.db.mark_published(ARTICLE, iso_utc())


@pytest.mark.parametrize("article_input", [ARTICLE, "#xg_abcdefgh"])
def test_report_with_id_asks_only_for_reason(ctx, bot, article_input):
    async def scenario():
        await seed(ctx)
        await make_pending(ctx)
        await report_command(bot.message(text=f"/report {article_input}"), bot, ctx)
        state = await ctx.db.get_state(42)
        assert state["state"] == "report_wait_reason" and state["payload"]["article_id"] == ARTICLE
        assert "請填寫檢舉原因" in bot.messages[-1]["text"]
        assert not await ctx.db.get_pending_for_user(42)
        assert not await ctx.db.list_open_reports(10, 0)
        await private_text(bot.message(text="檢舉原因"), bot, ctx)
        reports = await ctx.db.list_open_reports(10, 0)
        assert len(reports) == 1 and reports[0]["article_id"] == ARTICLE and reports[0]["reason"] == "檢舉原因"
        assert not await ctx.db.get_state(42)
    asyncio.run(scenario())


@pytest.mark.parametrize("article_input,deleted", [("不是ID", False), ("XG_BCDEFGHJ", False), (ARTICLE, True)])
def test_report_with_invalid_or_deleted_id_allows_correction(ctx, bot, article_input, deleted):
    async def scenario():
        await seed(ctx)
        if deleted:
            await ctx.db.mark_deleted(ARTICLE, iso_utc())
        await report_command(bot.message(text=f"/report {article_input}"), bot, ctx)
        assert (await ctx.db.get_state(42))["state"] == "report_wait_id"
        assert "檢舉原因（" not in bot.messages[-1]["text"]
        if deleted:
            await cancel_command(bot.message(text="/cancel"), bot, ctx)
            assert not await ctx.db.get_state(42)
        else:
            await private_text(bot.message(text=ARTICLE), bot, ctx)
            assert (await ctx.db.get_state(42))["state"] == "report_wait_reason"
        assert not await ctx.db.list_open_reports(10, 0)
        assert not await ctx.db.get_pending_for_user(42)
    asyncio.run(scenario())


def test_report_prompts_for_id_then_reason_without_creating_submission(ctx, bot):
    async def scenario():
        await seed(ctx)
        await make_pending(ctx)
        await report_command(bot.message(text="/report"), bot, ctx)
        assert (await ctx.db.get_state(42))["state"] == "report_wait_id"
        assert "文章 ID" in bot.messages[-1]["text"]
        assert not await ctx.db.get_pending_for_user(42)
        await private_text(bot.message(text="#xg_abcdefgh"), bot, ctx)
        assert (await ctx.db.get_state(42))["state"] == "report_wait_reason"
        assert "檢舉原因" in bot.messages[-1]["text"]
        assert not await ctx.db.list_open_reports(10, 0)
        await private_text(bot.message(text="delete"), bot, ctx)
        reports = await ctx.db.list_open_reports(10, 0)
        assert len(reports) == 1 and reports[0]["article_id"] == ARTICLE
        assert reports[0]["reason"] == "delete"
        assert not await ctx.db.get_state(42)
        assert not await ctx.db.get_pending_for_user(42)
        assert "已收到" in bot.messages[-2]["text"]
        assert bot.messages[-1]["chat_id"] == 99
    asyncio.run(scenario())


def test_report_retries_invalid_id_and_reason(ctx, bot):
    async def scenario():
        await seed(ctx)
        await report_command(bot.message(text="/report"), bot, ctx)
        for text in ("不是ID", "XG_BCDEFGHJ"):
            await private_text(bot.message(text=text), bot, ctx)
            assert (await ctx.db.get_state(42))["state"] == "report_wait_id"
        await private_text(bot.message(text=ARTICLE), bot, ctx)
        for text in ("  ", "字" * 501):
            await private_text(bot.message(text=text), bot, ctx)
            assert (await ctx.db.get_state(42))["state"] == "report_wait_reason"
            assert not await ctx.db.list_open_reports(10, 0)
        await private_text(bot.message(text="字" * 500), bot, ctx)
        assert len(await ctx.db.list_open_reports(10, 0)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["id", "reason"])
@pytest.mark.parametrize("exit_command", [cancel_command, start_command])
def test_report_can_be_cancelled_at_each_step(ctx, bot, stage, exit_command):
    async def scenario():
        await seed(ctx)
        await report_command(bot.message(text="/report"), bot, ctx)
        if stage == "reason":
            await private_text(bot.message(text=ARTICLE), bot, ctx)
        await exit_command(bot.message(text="/cancel"), bot, ctx)
        assert not await ctx.db.get_state(42)
        assert not await ctx.db.list_open_reports(10, 0)
    asyncio.run(scenario())


def test_report_rechecks_article_and_duplicate_on_submission(ctx, bot):
    async def scenario():
        await seed(ctx)
        for _ in range(2):
            await report_command(bot.message(text="/report"), bot, ctx)
            await private_text(bot.message(text=ARTICLE), bot, ctx)
            await private_text(bot.message(text="測試原因"), bot, ctx)
        assert len(await ctx.db.list_open_reports(10, 0)) == 1
        assert "已檢舉過" in bot.messages[-1]["text"]
        assert not await ctx.db.get_state(42)
        await report_command(bot.message(user_id=44, text="/report"), bot, ctx)
        await private_text(bot.message(user_id=44, text=ARTICLE), bot, ctx)
        ctx.db._conn().execute("UPDATE articles SET status = 'deleted' WHERE article_id = ?", (ARTICLE,))
        await private_text(bot.message(user_id=44, text="測試原因"), bot, ctx)
        assert "不存在或已刪除" in bot.messages[-1]["text"]
        assert not await ctx.db.get_state(44)
        assert len(await ctx.db.list_open_reports(10, 0)) == 1
    asyncio.run(scenario())
