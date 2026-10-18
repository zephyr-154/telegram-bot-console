from __future__ import annotations

import logging
import secrets
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.context import AppContext
from app.handlers.admin import update_admin_panel
from app.keyboards import (
    admin_article_keyboard,
    admin_back_keyboard,
    admin_keyboard,
    delete_keyboard,
    home_keyboard,
    submission_keyboard,
)
from app.messages import WELCOME_TEXT
from app.services.admin import format_article
from app.services.deletion import delete_article_messages
from app.services.publishing import publish_pending
from app.telegram_utils import delete_private_messages, try_delete_message
from app.utils import clean_submission_text, iso_utc, normalize_article_id, pending_expired

logger = logging.getLogger(__name__)
router = Router(name="private")


async def _clear_pending_messages(
    ctx: AppContext,
    bot: Bot,
    user_id: int,
    chat_id: int,
) -> None:
    pending = await ctx.db.delete_pending(user_id)
    if pending:
        await delete_private_messages(
            bot,
            chat_id,
            [pending.get("source_message_id"), pending.get("confirmation_message_id")],
        )


async def _cleanup_operation_messages(
    bot: Bot,
    chat_id: int,
    payload: dict[str, Any],
    *,
    keep_message_id: int | None = None,
) -> None:
    message_ids = [
        payload.get("command_message_id"),
        payload.get("prompt_message_id"),
        payload.get("article_input_message_id"),
        payload.get("confirmation_message_id"),
        payload.get("panel_message_id"),
    ]
    await delete_private_messages(
        bot,
        chat_id,
        [message_id for message_id in message_ids if message_id != keep_message_id],
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_command(message: Message, bot: Bot, ctx: AppContext) -> None:
    user_id = message.from_user.id
    if ctx.user_locks[user_id].locked():
        await message.answer("投稿正在處理，請稍候完成後再操作。")
        return
    async with ctx.user_locks[user_id]:
        await clear_user_operation(message, bot, ctx, user_id)
        await message.answer(WELCOME_TEXT, reply_markup=home_keyboard())


async def clear_user_operation(message: Message, bot: Bot, ctx: AppContext, user_id: int) -> None:
    old_state = await ctx.db.get_state(user_id)
    if old_state:
        await _cleanup_operation_messages(
            bot,
            message.chat.id,
            old_state["payload"],
            keep_message_id=message.message_id,
        )
        await ctx.db.clear_state(user_id)
    await _clear_pending_messages(ctx, bot, user_id, message.chat.id)


@router.message(Command("hhvszephyr"), F.chat.type == ChatType.PRIVATE)
async def admin_command(message: Message, bot: Bot, ctx: AppContext) -> None:
    if message.from_user.id != ctx.settings.admin_user_id:
        return
    user_id = message.from_user.id
    if ctx.user_locks[user_id].locked():
        await message.answer("目前有操作正在處理，請稍候。")
        return
    async with ctx.user_locks[user_id]:
        old_state = await ctx.db.get_state(user_id)
        if old_state:
            await _cleanup_operation_messages(bot, message.chat.id, old_state["payload"], keep_message_id=message.message_id)
            await ctx.db.clear_state(user_id)
        await try_delete_message(bot, message.chat.id, message.message_id)
        paused = await ctx.db.submissions_paused()
        panel = await message.answer("管理員面板\n投稿狀態：" + ("暫停" if paused else "開放"), reply_markup=admin_keyboard())
        await ctx.db.set_state(user_id, "admin_panel", {"panel_message_id": panel.message_id}, iso_utc())


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def private_text(message: Message, bot: Bot, ctx: AppContext) -> None:
    user_id = message.from_user.id
    text = message.text or ""
    stripped_text = text.strip()
    command_head = stripped_text.split(maxsplit=1)[0] if stripped_text else ""
    if command_head.casefold().split("@", 1)[0] == "/hhvszephyr":
        return
    if ctx.user_locks[user_id].locked():
        await message.answer("上一份投稿或操作正在處理，請完成後再傳送。")
        return

    if text.strip().casefold() == "delete":
        async with ctx.user_locks[user_id]:
            await clear_user_operation(message, bot, ctx, user_id)
            prompt = await message.answer("請貼上要刪除的文章 ID。輸入 /cancel 可取消。")
            await ctx.db.set_state(
                user_id, "delete_wait_id",
                {"command_message_id": message.message_id, "prompt_message_id": prompt.message_id},
                iso_utc(),
            )
        return

    if command_head.startswith("/"):
        await message.answer("未知指令，請輸入 /help 查看操作說明。")
        return

    state = await ctx.db.get_state(user_id)
    if state and state["state"].startswith("admin_") and user_id == ctx.settings.admin_user_id:
        await _handle_admin_text(message, bot, ctx, state)
        return
    if state and state["state"] == "delete_wait_id":
        async with ctx.user_locks[user_id]:
            await _handle_delete_id(message, bot, ctx, state["payload"])
        return
    if state and state["state"] == "delete_confirm":
        await message.answer("請使用上方按鈕確認或取消刪除。")
        return

    if await ctx.db.is_blocked(user_id):
        await message.answer("目前無法投稿。")
        return
    if await ctx.db.submissions_paused():
        await message.answer("目前暫停投稿，請稍後再試。")
        return
    try:
        text = clean_submission_text(text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    async with ctx.user_locks[user_id]:
        nonce = secrets.token_urlsafe(12)
        old = await ctx.db.replace_pending(
            user_id=user_id,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            raw_text=text,
            callback_nonce=nonce,
            created_at_utc=iso_utc(),
        )
        if old:
            await delete_private_messages(
                bot,
                message.chat.id,
                [old.get("source_message_id"), old.get("confirmation_message_id")],
            )
        try:
            try:
                preview = await message.answer(
                    f"確定要這樣發布嗎？\n\n{text}", reply_markup=submission_keyboard(nonce),
                )
            except TelegramBadRequest as exc:
                if "too long" not in str(exc).casefold():
                    raise
                preview = await message.answer(
                    "請確認你上方的原文，確定要發布嗎？", reply_markup=submission_keyboard(nonce),
                )
            await ctx.db.update_pending_confirmation(user_id, preview.message_id)
        except Exception:
            await ctx.db.delete_pending(user_id)
            raise


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_non_text(message: Message) -> None:
    if message.text is None:
        await message.answer("目前只接受純文字投稿。")


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("submit:"))
async def submission_callback(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"yes", "no"}:
        await callback.answer("操作已失效", show_alert=True)
        return
    action, nonce = parts[1], parts[2]
    user_id = callback.from_user.id
    pending = await ctx.db.get_pending_by_nonce(nonce)
    if (
        not pending
        or int(pending["user_id"]) != user_id
        or not callback.message
        or callback.message.chat.id != pending["source_chat_id"]
        or pending.get("confirmation_message_id") != callback.message.message_id
    ):
        await callback.answer("這個操作不屬於你或已失效", show_alert=True)
        return
    if ctx.user_locks[user_id].locked():
        await callback.answer("這份投稿正在處理，請稍候。", show_alert=True)
        return
    await callback.answer("已收到，正在排隊處理。" if action == "yes" else "")

    async with ctx.user_locks[user_id]:
        pending = await ctx.db.get_pending_by_nonce(nonce)
        if not pending:
            return
        if pending_expired(pending):
            await _clear_pending_messages(ctx, bot, user_id, callback.message.chat.id)
            await callback.message.answer("這份投稿已超過 24 小時確認期限，請重新投稿。")
            return
        if action == "no":
            await ctx.db.delete_pending(user_id)
            await delete_private_messages(
                bot,
                callback.message.chat.id,
                [pending.get("source_message_id"), pending.get("confirmation_message_id")],
            )
            await callback.message.answer("已取消發布")
            return
        if action != "yes":
            return

        try:
            success, article_id, error = await publish_pending(ctx, bot, pending)
        except Exception:
            logger.exception("publish failed user_id=%s", user_id)
            success, article_id, error = False, None, "發布時發生錯誤，請稍後重新投稿。"
        await ctx.db.delete_pending(user_id)
        await delete_private_messages(
            bot,
            callback.message.chat.id,
            [pending.get("confirmation_message_id")],
        )
        if success and article_id:
            await callback.message.answer(f"#{article_id}")
        else:
            await callback.message.answer(error or "發布失敗，請稍後再試。")


async def _handle_delete_id(
    message: Message,
    bot: Bot,
    ctx: AppContext,
    payload: dict[str, Any],
) -> None:
    article_id = normalize_article_id(message.text or "")
    if not article_id:
        await message.answer("文章 ID 格式不正確，請重新貼上文章 ID。")
        return
    await prompt_article_deletion(message, bot, ctx, payload, article_id, message.from_user.id)


async def prompt_article_deletion(
    message: Message, bot: Bot, ctx: AppContext, payload: dict, article_id: str, user_id: int,
) -> None:
    article = await ctx.db.get_article(article_id)
    operation_payload = {**payload, "article_input_message_id": message.message_id}
    if not article or article["status"] == "deleted":
        await ctx.db.clear_state(user_id)
        await _cleanup_operation_messages(bot, message.chat.id, operation_payload)
        await message.answer("文章不存在或已刪除。")
        return
    if int(article["author_user_id"]) != user_id:
        await ctx.db.clear_state(user_id)
        await _cleanup_operation_messages(bot, message.chat.id, operation_payload)
        await message.answer("只有原投稿者才能刪除這篇文章。")
        return

    nonce = secrets.token_urlsafe(12)
    confirmation = await message.answer(
        f"確定要刪除 #{article_id} 嗎？",
        reply_markup=delete_keyboard(nonce, article_id),
    )
    await ctx.db.set_state(
        user_id,
        "delete_confirm",
        {
            **operation_payload,
            "article_id": article_id,
            "nonce": nonce,
            "confirmation_message_id": confirmation.message_id,
        },
        iso_utc(),
    )


async def _handle_admin_text(
    message: Message,
    bot: Bot,
    ctx: AppContext,
    state: dict[str, Any],
) -> None:
    user_id = message.from_user.id
    panel_id = state.get("payload", {}).get("panel_message_id")
    try:
        panel_id = int(panel_id) if panel_id is not None else None
    except (TypeError, ValueError):
        panel_id = None

    async with ctx.user_locks[user_id]:
        current_state = await ctx.db.get_state(user_id)
        if not current_state or current_state.get("state") != state.get("state"):
            return
        current_panel_id = current_state.get("payload", {}).get("panel_message_id")
        try:
            current_panel_id = (
                int(current_panel_id) if current_panel_id is not None else panel_id
            )
        except (TypeError, ValueError):
            current_panel_id = panel_id

        if state["state"] == "admin_wait_article":
            article_id = normalize_article_id(message.text or "")
            if not article_id:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=message.chat.id,
                    current_panel_id=current_panel_id,
                    text="文章 ID 格式不正確。",
                    reply_markup=admin_back_keyboard(),
                    state_name="admin_wait_article",
                )
                await try_delete_message(bot, message.chat.id, message.message_id)
                return
            article = await ctx.db.get_article(article_id)
            article_messages = (
                await ctx.db.get_article_messages(article_id) if article else []
            )
            await try_delete_message(bot, message.chat.id, message.message_id)
            if not article:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=message.chat.id,
                    current_panel_id=current_panel_id,
                    text="找不到這篇文章。",
                    reply_markup=admin_back_keyboard(),
                )
                return

            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=message.chat.id,
                current_panel_id=current_panel_id,
                text=format_article(article, article_messages, ctx.settings.timezone),
                reply_markup=admin_article_keyboard(article_id, bool(article.get("publish_error_code") or article.get("delete_error_code"))),
            )
            return
        if state["state"] == "admin_wait_unblock":
            try:
                target_user_id = int((message.text or "").strip())
            except ValueError:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=message.chat.id,
                    current_panel_id=current_panel_id,
                    text="Telegram User ID 必須是整數。",
                    reply_markup=admin_back_keyboard(),
                    state_name="admin_wait_unblock",
                )
                await try_delete_message(bot, message.chat.id, message.message_id)
                return
            await ctx.db.unblock_user(target_user_id)
            await try_delete_message(bot, message.chat.id, message.message_id)
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=message.chat.id,
                current_panel_id=current_panel_id,
                text="已解除封鎖。",
                reply_markup=admin_keyboard(),
            )
            return
        if state["state"] == "admin_wait_block":
            try:
                target_user_id = int((message.text or "").strip())
            except ValueError:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=message.chat.id,
                    current_panel_id=current_panel_id,
                    text="Telegram User ID 必須是整數。",
                    reply_markup=admin_back_keyboard(),
                    state_name="admin_wait_block",
                )
                await try_delete_message(bot, message.chat.id, message.message_id)
                return
            await ctx.db.block_user(target_user_id, iso_utc(), "由管理員手動封鎖")
            await try_delete_message(bot, message.chat.id, message.message_id)
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=message.chat.id,
                current_panel_id=current_panel_id,
                text="已封鎖使用者。",
                reply_markup=admin_keyboard(),
            )


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("delete:"))
async def deletion_callback(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("操作已失效", show_alert=True)
        return
    _, action, article_id, nonce = parts
    state = await ctx.db.get_state(callback.from_user.id)
    if not state or state["state"] != "delete_confirm":
        await callback.answer("操作已失效", show_alert=True)
        return
    payload = state["payload"]
    if (
        payload.get("article_id") != article_id
        or payload.get("nonce") != nonce
        or not callback.message
        or callback.message.chat.id != callback.from_user.id
        or payload.get("confirmation_message_id") != callback.message.message_id
    ):
        await callback.answer("操作已失效", show_alert=True)
        return

    async with ctx.user_locks[callback.from_user.id]:
        current_state = await ctx.db.get_state(callback.from_user.id)
        if (
            not current_state
            or current_state["state"] != "delete_confirm"
            or not callback.message
            or current_state["payload"].get("confirmation_message_id")
            != callback.message.message_id
        ):
            await callback.answer("這個操作已完成或失效", show_alert=True)
            return
        payload = current_state["payload"]
        if payload.get("article_id") != article_id or payload.get("nonce") != nonce:
            await callback.answer("這個操作已失效", show_alert=True)
            return
        await callback.answer()
        await ctx.db.clear_state(callback.from_user.id)
        await _cleanup_operation_messages(bot, callback.message.chat.id, payload)
        if action == "no":
            await callback.message.answer("已取消刪除")
            return
        if action != "yes":
            return
        result = await delete_article_messages(ctx, bot, article_id)
        if result.success:
            await callback.message.answer(f"#{article_id} 已刪除")
        elif result.expired:
            await callback.message.answer(
                "這篇文章已超過 Telegram Bot 可自動刪除的期限，請聯絡管理員手動刪除。"
            )
        else:
            await callback.message.answer("文章刪除失敗，請聯絡管理員處理。")
