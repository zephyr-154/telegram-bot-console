from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from aiogram.types import Message

from app.context import AppContext
from app.services.admin import notify_admin
from app.utils import iso_utc, normalize_article_id, utc_now


async def submit_report(message: Message, bot: Bot, ctx: AppContext, article_id: str, reason: str) -> None:
    """Submit a validated report while holding the user's operation lock."""
    article = await ctx.db.get_article(article_id)
    if not article or article["status"] == "deleted":
        await message.answer("文章不存在或已刪除。")
        return
    now = utc_now()
    result = await ctx.db.create_report(
        article_id, message.from_user.id, reason, iso_utc(now), iso_utc(now - timedelta(minutes=10)),
    )
    if result == "duplicate":
        await message.answer("你已檢舉過這篇文章，請等待管理員處理。")
    elif result == "limited":
        await message.answer("檢舉太頻繁，每人 10 分鐘最多 3 件，請稍後再試。")
    else:
        await message.answer(f"已收到 #{article_id} 的檢舉，將由管理員處理。")
        await notify_admin(ctx, bot, f"收到 #{article_id} 的檢舉，請查看管理面板的待處理事項。")


async def handle_report_input(message: Message, bot: Bot, ctx: AppContext, state: dict) -> None:
    """Advance the report conversation without creating a submission draft."""
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if state["state"] == "report_wait_id":
        article_id = normalize_article_id(text)
        if not article_id:
            await message.answer("請填寫正確的文章 ID，例如 XG_ABCDEFGH。輸入 /cancel 可取消。")
            return
        article = await ctx.db.get_article(article_id)
        if not article or article["status"] == "deleted":
            await message.answer("文章不存在或已刪除，請重新輸入文章 ID。輸入 /cancel 可取消。")
            return
        await ctx.db.set_state(user_id, "report_wait_reason", {"article_id": article_id}, iso_utc())
        await message.answer(f"要檢舉的文章：#{article_id}\n請填寫檢舉原因（1 至 500 字）。輸入 /cancel 可取消。")
        return
    if not text or len(text) > 500:
        await message.answer("檢舉原因需為 1 至 500 字，請重新填寫。輸入 /cancel 可取消。")
        return
    await submit_report(message, bot, ctx, state["payload"]["article_id"], text)
    await ctx.db.clear_state(user_id)
