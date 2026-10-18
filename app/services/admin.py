from __future__ import annotations

import logging
import json
from datetime import timedelta, timezone

from aiogram import Bot

from app.context import AppContext
from app.utils import article_status_text, iso_utc, message_link, taipei_now

logger = logging.getLogger(__name__)


def format_report(report: dict) -> str:
    reply = {"none": "未回覆", "sending": "正在傳送，請稍候", "sent": "已回覆檢舉人",
             "failed": "回覆未送出，可重新回覆", "uncertain": "回覆結果不明，不會自動重送"}[report["reply_status"]]
    return (f"檢舉 #{report['report_id']}\n文章：#{report['article_id']}\n"
            f"狀態：{'待處理' if report['status'] == 'open' else '已結案'}\n回覆：{reply}\n\n檢舉原因：\n{report['reason']}")


def format_report_reply(report: dict, text: str) -> str:
    return f"關於你對 #{report['article_id']} 的檢舉，管理員回覆：\n\n{text}"


async def admin_stats(ctx: AppContext) -> tuple[int, int]:
    now = taipei_now(ctx.settings.timezone)
    now_utc = now.astimezone(timezone.utc)
    query_end = now_utc + timedelta(seconds=1)
    local_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc)
    total = await ctx.db.count_published_between(
        "0001-01-01T00:00:00+00:00",
        iso_utc(query_end),
    )
    today = await ctx.db.count_published_between(
        iso_utc(start_utc),
        iso_utc(query_end),
    )
    return today, total


async def notify_admin(ctx: AppContext, bot: Bot, text: str) -> None:
    for user_id in ctx.settings.admin_user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception as exc:
            logger.warning("admin notification failed error=%s", type(exc).__name__)


def format_article(article: dict, messages: list[dict], timezone_name: str) -> str:
    ids = ", ".join(str(row["message_id"]) for row in messages) or "未取得"
    links = {}
    for row in messages:
        links.setdefault(row["channel_id"], message_link(row["channel_id"], row["message_id"]))
    try:
        targets = json.loads(article.get("target_chat_ids") or "[]")
    except (TypeError, ValueError):
        targets = []
    return (
        f"文章：#{article['article_id']}\n"
        f"狀態：{article_status_text(article, timezone_name)}\n"
        f"原投稿者 Telegram User ID：{article['author_user_id']}\n"
        f"發布時間 UTC：{article.get('published_at_utc') or '尚未確認'}\n"
        f"投稿目標：{', '.join(map(str, targets)) or '舊紀錄，請依訊息位置核對'}\n"
        f"群組／頻道 message ID：{ids}\n"
        f"需要人工核對：{'是' if article.get('publish_error_code') or article.get('delete_error_code') else '否'}"
        + "".join(f"\n目標 {channel_id}：{link}" for channel_id, link in links.items() if link)
    )
