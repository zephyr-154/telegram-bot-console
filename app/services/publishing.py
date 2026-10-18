from __future__ import annotations

import asyncio
import logging
import json
import sqlite3
import time
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message

from app.context import AppContext
from app.image_renderer import ImageLayoutError, render_post_images
from app.services.admin import notify_admin
from app.services.rate_limit import check_publish_limits
from app.telegram_utils import try_delete_message
from app.utils import clean_submission_text, content_hmac, generate_article_id, iso_utc, pending_expired, taipei_now, utc_now

logger = logging.getLogger(__name__)


async def send_rendered_images(
    bot: Bot,
    channel_id: int,
    article_id: str,
    image_pages: list[bytes],
    timezone_name: str,
) -> list[Message]:
    if not 1 <= len(image_pages) <= 10:
        raise ValueError("圖片數量必須介於 1 到 10 張")
    now = taipei_now(timezone_name)
    caption = f"#{article_id}\n{now.month}月{now.day}日"
    if len(image_pages) == 1:
        message = await bot.send_photo(
            chat_id=channel_id,
            photo=BufferedInputFile(
                image_pages[0], filename=f"{article_id}_01.png"
            ),
            caption=caption,
        )
        return [message]
    media = [
        InputMediaPhoto(
            media=BufferedInputFile(
                page, filename=f"{article_id}_{index:02d}.png"
            ),
            caption=caption if index == 1 else None,
        )
        for index, page in enumerate(image_pages, start=1)
    ]
    return list(await bot.send_media_group(chat_id=channel_id, media=media))


async def cleanup_channel_messages(
    bot: Bot,
    channel_id: int,
    messages: list[Message],
) -> bool:
    all_deleted = True
    for message in messages:
        if not await try_delete_message(bot, channel_id, message.message_id):
            all_deleted = False
    return all_deleted


class _PublicationStopped(Exception):
    pass


async def _rollback_delivered(ctx: AppContext, bot: Bot, article_id: str, delivered: dict[int, list[Message]]) -> bool:
    all_deleted, offset = True, 0
    for channel_id, messages in delivered.items():
        try:
            await ctx.db.save_article_messages(article_id, channel_id, [message.message_id for message in messages], offset)
        except Exception:
            logger.error("could not save sent message IDs article=%s", article_id)
        if not await cleanup_channel_messages(bot, channel_id, messages):
            all_deleted = False
        offset += len(messages)
    return all_deleted


async def publish_pending(
    ctx: AppContext,
    bot: Bot,
    pending: dict,
) -> tuple[bool, str | None, str | None]:
    task = asyncio.current_task()
    if task:
        ctx.publish_tasks.add(task)
    article_id = None
    send_in_flight = False
    delivered: dict[int, list[Message]] = {}
    try:
        user_id = int(pending["user_id"])
        text = clean_submission_text(pending["raw_text"])
        nonce = pending["callback_nonce"]
        digest = content_hmac(user_id, text, ctx.settings.content_hash_secret)
        async with ctx.publish_lock:
            previous = await ctx.db.get_article_by_nonce(nonce)
            if previous:
                if previous["status"] == "published":
                    return True, previous["article_id"], None
                return False, previous["article_id"], f"#{previous['article_id']} 已處理或等待管理員核對，請勿重複發布。"
            error = await _publication_error(ctx, pending)
            if error:
                return False, None, error
            if await ctx.db.has_recent_duplicate(user_id, digest, iso_utc(utc_now() - timedelta(hours=24))):
                return False, None, "相同內容在 24 小時內已發布，或有發布結果等待核對，請勿重複投稿。"
            limits = await check_publish_limits(ctx.db, user_id, ctx.settings.timezone)
            if not limits.allowed:
                return False, None, limits.message

            image_pages = await asyncio.to_thread(
                render_post_images, text,
                regular_font_path=ctx.settings.font_regular_path,
                bold_font_path=ctx.settings.font_bold_path,
                logo_path=ctx.settings.telegram_logo_path,
            )
            for _ in range(5):
                candidate = generate_article_id()
                try:
                    await ctx.db.create_article(candidate, user_id, digest, nonce, ctx.settings.channel_ids)
                    article_id = candidate
                    break
                except sqlite3.IntegrityError:
                    if await ctx.db.get_article(candidate) is None:
                        raise
            if article_id is None:
                return False, None, "目前無法產生文章 ID，請稍後再試。"

            for channel_id in ctx.settings.channel_ids:
                while True:
                    error = await _publication_error(ctx, pending)
                    if error:
                        raise _PublicationStopped(error)
                    await _wait_for_send_slot(ctx, len(image_pages))
                    error = await _publication_error(ctx, pending)
                    if error:
                        raise _PublicationStopped(error)
                    await ctx.db.mark_send_started(article_id)
                    send_in_flight = True
                    try:
                        messages = await send_rendered_images(bot, channel_id, article_id, image_pages, ctx.settings.timezone)
                        send_in_flight = False
                        offset = sum(len(previous_messages) for previous_messages in delivered.values())
                        delivered[channel_id] = messages
                        await ctx.db.save_article_messages(article_id, channel_id, [message.message_id for message in messages], offset)
                        break
                    except TelegramRetryAfter as exc:
                        send_in_flight = False
                        if not delivered:
                            await ctx.db.clear_send_started(article_id)
                        remaining = exc.retry_after + 0.1
                        while remaining > 0:
                            error = await _publication_error(ctx, pending)
                            if error:
                                raise _PublicationStopped(error)
                            delay = min(remaining, 60)
                            await asyncio.sleep(delay)
                            remaining -= delay
            first_message = next(iter(delivered.values()))[0]
            await ctx.db.mark_published(article_id, iso_utc(getattr(first_message, "date", None)))
            return True, article_id, None
    except ImageLayoutError:
        return False, None, "內容或換行過多，無法排成 10 張圖片，請縮短內容或減少空行後重新投稿。"
    except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError, _PublicationStopped) as exc:
        rolled_back = not delivered or await _rollback_delivered(ctx, bot, article_id, delivered)
        if article_id and not rolled_back:
            await ctx.db.mark_publish_uncertain(article_id, "partial_publish")
            await notify_admin(ctx, bot, f"#{article_id} 部分目標已送出但撤回未完成，請核對所有目標群組／頻道。")
            return False, article_id, f"#{article_id} 尚有群組副本需要管理員核對，請勿重複投稿。"
        if article_id:
            await ctx.db.delete_article(article_id)
        if isinstance(exc, _PublicationStopped):
            return False, None, str(exc) + ("已撤回已送出的群組副本。" if delivered else "")
        logger.warning("publish rejected error=%s", type(exc).__name__)
        await notify_admin(ctx, bot, "投稿被 Telegram 拒絕，已撤回可確認的副本，請檢查各目標的發文權限與圖片設定。")
        return False, None, "Telegram 拒絕發布，已通知管理員檢查權限或設定；原文仍保留在你的私訊中。"
    except asyncio.CancelledError:
        if article_id and (send_in_flight or delivered):
            await ctx.db.mark_publish_uncertain(article_id, "interrupted_publish")
        elif article_id:
            await ctx.db.delete_article(article_id)
        raise
    except Exception as exc:
        if article_id and (send_in_flight or delivered):
            rolled_back = await _rollback_delivered(ctx, bot, article_id, delivered)
            if not send_in_flight and rolled_back:
                await ctx.db.delete_article(article_id)
                return False, None, "發布後資料儲存失敗，已撤回貼文，請稍後重新投稿。"
            try:
                await ctx.db.mark_publish_uncertain(article_id, "unknown_result")
            except Exception:
                logger.error("could not mark uncertain publication article=%s", article_id)
            logger.warning("publish result uncertain article=%s error=%s", article_id, type(exc).__name__)
            await notify_admin(ctx, bot, f"#{article_id} 發布結果待核對，請查看管理面板並檢查所有目標群組／頻道。")
            return False, article_id, f"#{article_id} 發布結果尚未確認，已通知管理員核對。相同內容暫時不能重投，以免重複發文。"
        if article_id:
            await ctx.db.delete_article(article_id)
        raise
    finally:
        if task:
            ctx.publish_tasks.discard(task)


async def _publication_error(ctx: AppContext, pending: dict) -> str | None:
    if pending_expired(pending):
        return "這份投稿已超過 24 小時確認期限，請重新投稿。"
    try:
        targets = json.loads(pending.get("target_chat_ids") or "[]")
    except (TypeError, ValueError):
        targets = None
    if (not isinstance(targets, list) or any(type(target) is not int or target >= 0 for target in targets)
            or targets and set(targets) != set(ctx.settings.channel_ids)
            or not targets and len(ctx.settings.channel_ids) > 1):
        return "投稿目標已變更或舊稿未確認多群組發布，請重新傳送投稿確認。"
    if await ctx.db.submissions_paused():
        return "目前暫停投稿，請稍後再試。"
    if await ctx.db.is_blocked(int(pending["user_id"])):
        return "目前無法投稿。"
    return None


async def _wait_for_send_slot(ctx: AppContext, image_count: int) -> None:
    # shortcut: 節流由單一 Bot 程序維護，多程序部署前須改用共用佇列。
    while True:
        now = time.monotonic()
        while ctx.send_times and now - ctx.send_times[0] >= 60:
            ctx.send_times.popleft()
        delay = max(0.0, ctx.send_times[-1] + 1.1 - now) if ctx.send_times else 0.0
        excess = len(ctx.send_times) + image_count - 20
        if excess > 0:
            delay = max(delay, ctx.send_times[excess - 1] + 60.1 - now)
        if delay <= 0:
            ctx.send_times.extend([now] * image_count)
            return
        await asyncio.sleep(min(delay, 60))
