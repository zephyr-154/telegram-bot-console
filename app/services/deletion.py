from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot

from app.context import AppContext
from app.services.admin import notify_admin
from app.telegram_utils import try_delete_message
from app.utils import iso_utc, parse_datetime, utc_now


@dataclass(frozen=True, slots=True)
class DeleteResult:
    success: bool
    expired: bool = False
    error_code: str | None = None


async def delete_article_messages(
    ctx: AppContext,
    bot: Bot,
    article_id: str,
) -> DeleteResult:
    async with ctx.article_locks[article_id]:
        article = await ctx.db.get_article(article_id)
        if not article:
            return DeleteResult(False, error_code="not_found")
        if article["status"] == "deleted":
            return DeleteResult(False, error_code="already_deleted")
        if article["status"] == "publishing":
            return DeleteResult(False, error_code="publication_uncertain" if article.get("publish_error_code") else "publication_in_progress")

        published_at = parse_datetime(article.get("published_at_utc"))
        if published_at and utc_now() - published_at >= timedelta(hours=48):
            await ctx.db.mark_delete_failed(article_id, iso_utc(), "older_than_48_hours")
            if not article.get("delete_error_code"):
                await notify_admin(ctx, bot, f"#{article_id} 刪文已超過 Bot 期限，請查看管理面板的待處理事項。")
            return DeleteResult(False, expired=True, error_code="older_than_48_hours")

        rows = await ctx.db.get_article_messages(article_id)
        if not rows:
            await ctx.db.mark_delete_failed(article_id, iso_utc(), "message_ids_missing")
            if not article.get("delete_error_code"):
                await notify_admin(ctx, bot, f"#{article_id} 缺少貼文訊息 ID，需要人工核對。")
            return DeleteResult(False, error_code="message_ids_missing")

        failures = 0
        for row in rows:
            if not await try_delete_message(bot, row["channel_id"], row["message_id"]):
                failures += 1
        if failures:
            await ctx.db.mark_delete_failed(article_id, iso_utc(), "telegram_delete_failed")
            if not article.get("delete_error_code"):
                await notify_admin(ctx, bot, f"#{article_id} 刪除失敗，請查看管理面板的待處理事項。")
            return DeleteResult(False, error_code="telegram_delete_failed")

        await ctx.db.mark_deleted(article_id, iso_utc())
        return DeleteResult(True)
