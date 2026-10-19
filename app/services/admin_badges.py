from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.context import AppContext

logger = logging.getLogger(__name__)


def with_attention_badges(keyboard: InlineKeyboardMarkup, issues: int, reports: int, *,
                          represented: tuple[int, int] = (0, 0), item_counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    keyboard = keyboard.model_copy(deep=True)
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row
                 if not button.text.startswith("返回") and button.text.split(" 🔴 ", 1)[0] not in {"上一頁", "下一頁"}}
    covered_issues = issues if "admin:issues:0" in callbacks else represented[0]
    covered_reports = reports if "admin:reports:0" in callbacks else represented[1]
    elsewhere = max(0, issues - covered_issues) + max(0, reports - covered_reports)
    counts = {"admin:pending": issues + reports, "admin:back": elsewhere,
              "admin:issues:0": issues, "admin:reports:0": reports, **(item_counts or {})}
    for row in keyboard.inline_keyboard:
        for button in row:
            if button.callback_data in counts:
                label = button.text.split(" 🔴 ", 1)[0]
                count = counts[button.callback_data]
                if label in {"上一頁", "下一頁"}:
                    count = 0
                if label.startswith("返回"):
                    if button.callback_data == "admin:pending":
                        count = elsewhere
                    elif button.callback_data == "admin:reports:0":
                        count = max(0, reports - covered_reports)
                    elif button.callback_data == "admin:issues:0":
                        count = max(0, issues - covered_issues)
                button.text = f"{label} 🔴 {count}" if count else label
    return keyboard


async def decorate_admin_keyboard(ctx: AppContext, keyboard: InlineKeyboardMarkup, payload: dict | None = None) -> InlineKeyboardMarkup:
    issues, reports = await ctx.db.attention_counts()
    payload = payload or {}
    scope = payload.get("badge_scope")
    represented = (0, 0)
    if scope == "issues":
        represented = (issues, 0)
    elif scope == "reports":
        represented = (0, await ctx.db.count_open_reports(payload.get("badge_article_id")))
    elif payload.get("report_id"):
        report = await ctx.db.get_report(payload["report_id"])
        represented = (0, int(bool(report and report["status"] == "open")))
    elif payload.get("article_id"):
        article = await ctx.db.get_article(payload["article_id"])
        represented = (int(bool(article and (article.get("publish_error_code") or article.get("delete_error_code")))), 0)
    item_counts = {}
    for row in keyboard.inline_keyboard:
        for button in row:
            parts = (button.callback_data or "").split(":")
            if len(parts) == 3 and parts[:2] == ["admin", "report"] and parts[2].isdigit():
                if button.text.startswith("處理檢舉 #"):
                    report = await ctx.db.get_report(int(parts[2]))
                    item_counts[button.callback_data] = int(bool(report and report["status"] == "open"))
                else:
                    item_counts[button.callback_data] = 0
            elif len(parts) == 3 and parts[:2] == ["admin", "review"] and scope == "issues":
                article = await ctx.db.get_article(parts[2])
                item_counts[button.callback_data] = int(bool(article and (article.get("publish_error_code") or article.get("delete_error_code"))))
            elif len(parts) == 4 and parts[:2] == ["admin", "article_reports"] and button.text.startswith("處理此文"):
                count = await ctx.db.count_open_reports(parts[2])
                item_counts[button.callback_data] = count
                represented = (represented[0], count)
    # Old open lists can be refreshed after deployment before their next click.
    if not scope and any(button.text.startswith("處理檢舉 #") for row in keyboard.inline_keyboard for button in row):
        represented = (0, reports)
    return with_attention_badges(keyboard, issues, reports, represented=represented, item_counts=item_counts)


async def refresh_admin_badges(ctx: AppContext, bot: Bot) -> None:
    for user_id in ctx.settings.admin_user_ids:
        if ctx.user_locks[user_id].locked():
            continue
        async with ctx.user_locks[user_id]:
            state = await ctx.db.get_state(user_id)
            payload = state["payload"] if state and state["state"].startswith("admin_") else {}
            panel_id, saved_keyboard = payload.get("panel_message_id"), payload.get("panel_keyboard")
            if not panel_id or not saved_keyboard:
                ctx.admin_badge_cache.pop(user_id, None)
                continue
            original = InlineKeyboardMarkup.model_validate(saved_keyboard)
            updated = await decorate_admin_keyboard(ctx, original, payload)
            signature = updated.model_dump_json()
            previous = ctx.admin_badge_cache.get(user_id, (panel_id, original.model_dump_json()))
            if previous == (panel_id, signature):
                continue
            try:
                await bot.edit_message_reply_markup(chat_id=user_id, message_id=panel_id, reply_markup=updated, request_timeout=5)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    logger.warning("admin badge refresh failed error=%s", type(exc).__name__)
                    continue
            except (TelegramAPIError, TimeoutError, OSError) as exc:
                logger.warning("admin badge refresh failed error=%s", type(exc).__name__)
                continue
            # Do not change the operation state, nonce, or expiry when updating a badge.
            ctx.admin_badge_cache[user_id] = (panel_id, signature)


async def watch_admin_badges(ctx: AppContext, bot: Bot) -> None:
    while True:
        try:
            await refresh_admin_badges(ctx, bot)
        except Exception as exc:
            logger.warning("admin badge refresh failed error=%s", type(exc).__name__)
        await asyncio.sleep(5)
