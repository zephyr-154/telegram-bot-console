from __future__ import annotations

import asyncio
import logging
import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.backup import backup_database
from app.context import AppContext
from app.keyboards import admin_article_keyboard, admin_back_keyboard, admin_force_keyboard, admin_keyboard, admin_resolution_keyboard, page_buttons
from app.services.admin import admin_stats, format_article
from app.services.deletion import delete_article_messages
from app.telegram_utils import try_delete_message
from app.utils import iso_utc, normalize_article_id

router = Router(name="admin_callbacks")
logger = logging.getLogger(__name__)


def _is_admin(callback: CallbackQuery, ctx: AppContext) -> bool:
    return callback.from_user.id == ctx.settings.admin_user_id


def _panel_message_id(state: dict | None) -> int | None:
    if not state:
        return None
    raw_value = state.get("payload", {}).get("panel_message_id")
    try:
        return int(raw_value) if raw_value is not None else None
    except (TypeError, ValueError):
        return None


async def update_admin_panel(
    bot: Bot,
    ctx: AppContext,
    *,
    user_id: int,
    chat_id: int,
    current_panel_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    state_name: str = "admin_panel",
    state_payload: dict | None = None,
) -> int:
    """Edit the current admin panel, replacing it only if Telegram removed it."""
    panel_message_id = current_panel_id
    if panel_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as exc:
            if "message is not modified" not in str(exc).casefold():
                panel_message_id = None
        if panel_message_id is not None:
            await ctx.db.set_state(
                user_id,
                state_name,
                {"panel_message_id": panel_message_id, **(state_payload or {})},
                iso_utc(),
            )
            return panel_message_id

    panel = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )
    if current_panel_id is not None:
        await try_delete_message(bot, chat_id, current_panel_id)
    panel_message_id = panel.message_id
    await ctx.db.set_state(
        user_id,
        state_name,
        {"panel_message_id": panel_message_id, **(state_payload or {})},
        iso_utc(),
    )
    return panel_message_id


async def _get_current_admin_state(
    callback: CallbackQuery,
    ctx: AppContext,
) -> dict | None:
    if not callback.message or callback.message.chat.id != ctx.settings.admin_user_id:
        return None
    state = await ctx.db.get_state(callback.from_user.id)
    if _panel_message_id(state) != callback.message.message_id:
        return None
    return state


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not _is_admin(callback, ctx):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = callback.from_user.id
    state = await _get_current_admin_state(callback, ctx)
    if state is None:
        await callback.answer("這個管理面板已失效，請重新輸入管理員指令。", show_alert=True)
        return
    if not callback.message:
        await callback.answer("操作已失效", show_alert=True)
        return

    await callback.answer()
    chat_id = callback.message.chat.id
    panel_id = callback.message.message_id

    async with ctx.user_locks[user_id]:
        # A second click can arrive while the first callback is doing a DB or
        # Telegram request. Re-read state after acquiring the lock.
        state = await _get_current_admin_state(callback, ctx)
        if state is None:
            return

        if action == "back" and len(parts) == 2:
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text="管理員面板",
                reply_markup=admin_keyboard(),
            )
            return
        if action == "stats" and len(parts) == 2:
            today, total = await admin_stats(ctx)
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text=f"今日成功投稿：{today}\n歷史成功投稿：{total}",
                reply_markup=admin_back_keyboard(),
            )
            return
        if action == "search" and len(parts) == 2:
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text="請輸入要查詢的文章 ID。",
                reply_markup=admin_back_keyboard(),
                state_name="admin_wait_article",
            )
            return
        if action == "unblock" and len(parts) == 2:
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text="請輸入要解除封鎖的 Telegram User ID。",
                reply_markup=admin_back_keyboard(),
                state_name="admin_wait_unblock",
            )
            return
        if action == "block" and len(parts) == 2:
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text="請輸入要封鎖的 Telegram User ID。",
                reply_markup=admin_back_keyboard(),
                state_name="admin_wait_block",
            )
            return
        if action == "list" and len(parts) in {2, 3}:
            if len(parts) == 3 and (not parts[2].isdigit() or len(parts[2]) > 6):
                return
            page = int(parts[2]) if len(parts) == 3 else 0
            rows = await ctx.db.list_blocked_users(21, page * 20)
            if not rows:
                text = "目前沒有封鎖中的使用者。" if page == 0 else "此頁沒有封鎖紀錄，請返回上一頁。"
            else:
                text = f"封鎖名單（第 {page + 1} 頁）：\n" + "\n".join(
                    f"{row['user_id']}（{row['blocked_at_utc']}）" for row in rows[:20]
                )
            keyboard = admin_back_keyboard()
            navigation = page_buttons("admin:list", page, len(rows) > 20)
            if navigation:
                keyboard.inline_keyboard.insert(0, navigation)
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        if action == "pause" and len(parts) == 2:
            paused = await ctx.db.submissions_paused()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="暫停投稿", callback_data="admin:set_pause:1"),
                 InlineKeyboardButton(text="恢復投稿", callback_data="admin:set_pause:0")],
                [InlineKeyboardButton(text="返回管理面板", callback_data="admin:back")],
            ])
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text="目前投稿狀態：" + ("暫停" if paused else "開放"), reply_markup=keyboard,
            )
            return
        if action == "set_pause" and len(parts) == 3 and parts[2] in {"0", "1"}:
            paused = parts[2] == "1"
            await ctx.db.set_submissions_paused(paused)
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text="投稿已暫停；檢舉與刪文仍可使用。" if paused else "投稿已恢復。",
                reply_markup=admin_keyboard(),
            )
            return
        if action == "backup" and len(parts) == 2:
            try:
                path = await asyncio.to_thread(
                    backup_database, ctx.db.path, ctx.db.path.parent / "backups",
                )
                text = f"備份完成：{path.name}\n備份存於資料庫旁的 backups 目錄，保留最近 7 份。"
            except Exception as exc:
                logger.warning("backup failed error=%s", type(exc).__name__)
                text = "備份失敗，請檢查磁碟空間、資料庫及備份目錄權限。"
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text=text, reply_markup=admin_back_keyboard(),
            )
            return
        if action == "pending" and len(parts) == 2:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="發布／刪文待處理", callback_data="admin:issues:0")],
                [InlineKeyboardButton(text="檢舉待處理", callback_data="admin:reports:0")],
                [InlineKeyboardButton(text="返回管理面板", callback_data="admin:back")],
            ])
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text="待處理事項：請選擇分類。", reply_markup=keyboard,
            )
            return
        if action in {"issues", "reports"} and len(parts) == 3:
            if not parts[2].isdigit() or len(parts[2]) > 6:
                return
            page = int(parts[2])
            size = 10 if action == "issues" else 3
            if action == "issues":
                rows = await ctx.db.list_attention_articles(size + 1, page * size)
            else:
                rows = await ctx.db.list_open_reports(size + 1, page * size)
            lines = [f"{'發布／刪文' if action == 'issues' else '檢舉'}待處理（第 {page + 1} 頁）"]
            buttons = []
            for row in rows[:size]:
                article_id = row["article_id"]
                if action == "issues":
                    detail = "發布結果待核對" if row.get("publish_error_code") else "刪除失敗，需人工處理"
                else:
                    detail = f"檢舉 #{row['report_id']}：{row['reason']}"
                lines.append(f"\n#{article_id}\n{detail}")
                buttons.append([InlineKeyboardButton(text=f"處理 #{article_id}", callback_data=f"admin:review:{article_id}")])
            if not rows:
                lines.append("目前沒有待處理紀錄。")
            navigation = page_buttons(f"admin:{action}", page, len(rows) > size)
            if navigation:
                buttons.append(navigation)
            buttons.append([InlineKeyboardButton(text="返回待處理事項", callback_data="admin:pending")])
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text="\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            return
        if action in {"review", "close_reports", "resolve"} and len(parts) == 3:
            article_id = normalize_article_id(parts[2])
            article = await ctx.db.get_article(article_id) if article_id else None
            if not article:
                return
            if action == "close_reports":
                await ctx.db.close_reports(article_id)
                text = f"#{article_id} 的檢舉已結案，紀錄仍保留。"
                keyboard = admin_back_keyboard()
                state_name, state_payload = "admin_panel", None
            elif action == "resolve":
                if not (article.get("publish_error_code") or article.get("delete_error_code")):
                    return
                nonce = secrets.token_urlsafe(12)
                text = (
                    f"請先到目標群組／頻道搜尋 #{article_id}。若仍有貼文，先手動刪除全部圖片。\n\n"
                    "確認目標內已無此文後才可結案。未確認發布的稿件結案後可重新投稿；"
                    "已成功發布的文章仍適用原本的 24 小時重複限制。"
                )
                keyboard = admin_resolution_keyboard(article_id, nonce)
                state_name, state_payload = "admin_resolve_confirm", {"article_id": article_id, "nonce": nonce}
            else:
                messages = await ctx.db.get_article_messages(article_id)
                text = format_article(article, messages, ctx.settings.timezone)
                keyboard = admin_article_keyboard(article_id, bool(article.get("publish_error_code") or article.get("delete_error_code")))
                state_name, state_payload = "admin_panel", None
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text=text, reply_markup=keyboard, state_name=state_name, state_payload=state_payload,
            )
            return
        if action == "resolve_yes" and len(parts) == 4:
            article_id = normalize_article_id(parts[2])
            payload = state.get("payload", {})
            if (
                not article_id or state.get("state") != "admin_resolve_confirm"
                or payload.get("article_id") != article_id or payload.get("nonce") != parts[3]
            ):
                return
            async with ctx.article_locks[article_id]:
                article = await ctx.db.get_article(article_id)
                if not article or not (article.get("publish_error_code") or article.get("delete_error_code")):
                    return
                await ctx.db.mark_deleted(article_id, iso_utc())
                await ctx.db.close_reports(article_id)
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text=f"#{article_id} 已依管理員人工核對結果結案。", reply_markup=admin_back_keyboard(),
            )
            return
        if action == "force" and len(parts) == 3:
            article_id = normalize_article_id(parts[2])
            if not article_id:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=chat_id,
                    current_panel_id=panel_id,
                    text="文章 ID 格式不正確。",
                    reply_markup=admin_back_keyboard(),
                )
                return
            article = await ctx.db.get_article(article_id)
            if not article:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=chat_id,
                    current_panel_id=panel_id,
                    text="找不到這篇文章。",
                    reply_markup=admin_back_keyboard(),
                )
                return
            nonce = secrets.token_urlsafe(12)
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text=f"確定要強制刪除 #{article_id} 嗎？",
                reply_markup=admin_force_keyboard(article_id, nonce),
                state_name="admin_force_confirm",
                state_payload={"article_id": article_id, "nonce": nonce},
            )
            return
        if action == "force_yes" and len(parts) == 4:
            article_id = normalize_article_id(parts[2])
            nonce = parts[3]
            payload = state.get("payload", {})
            if (
                state.get("state") != "admin_force_confirm"
                or not article_id
                or payload.get("article_id") != article_id
                or payload.get("nonce") != nonce
            ):
                return
            result = await delete_article_messages(ctx, bot, article_id)
            if result.success:
                await ctx.db.close_reports(article_id)
                text = f"#{article_id} 已刪除。"
            elif result.expired:
                text = "這篇文章已超過 Telegram Bot 可自動刪除的期限，資料仍保留供人工處理。"
            else:
                text = "文章刪除失敗，資料仍保留供人工處理。"
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text=text,
                reply_markup=admin_back_keyboard(),
            )
            return
        if action == "block" and len(parts) == 3:
            article_id = normalize_article_id(parts[2])
            if not article_id:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=chat_id,
                    current_panel_id=panel_id,
                    text="文章 ID 格式不正確。",
                    reply_markup=admin_back_keyboard(),
                )
                return
            article = await ctx.db.get_article(article_id)
            if not article:
                await update_admin_panel(
                    bot,
                    ctx,
                    user_id=user_id,
                    chat_id=chat_id,
                    current_panel_id=panel_id,
                    text="找不到這篇文章。",
                    reply_markup=admin_back_keyboard(),
                )
                return
            await ctx.db.block_user(
                article["author_user_id"],
                iso_utc(),
                "由管理員從文章查詢封鎖",
            )
            await update_admin_panel(
                bot,
                ctx,
                user_id=user_id,
                chat_id=chat_id,
                current_panel_id=panel_id,
                text="已封鎖文章作者。",
                reply_markup=admin_back_keyboard(),
            )
            return
