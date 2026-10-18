from __future__ import annotations

import asyncio
import logging
import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.backup import backup_database
from app.context import AppContext
from app.keyboards import admin_article_keyboard, admin_back_keyboard, admin_force_keyboard, admin_keyboard, admin_report_keyboard, admin_resolution_keyboard, page_buttons
from app.services.admin import admin_stats, format_article, format_report, format_report_reply
from app.services.deletion import delete_article_messages
from app.services.publishing import _wait_for_send_slot
from app.telegram_utils import try_delete_message
from app.utils import iso_utc, message_link, normalize_article_id

router = Router(name="admin_callbacks")
logger = logging.getLogger(__name__)


def _is_admin(callback: CallbackQuery, ctx: AppContext) -> bool:
    return callback.from_user.id in ctx.settings.admin_user_ids


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
    if not callback.message or callback.message.chat.id != callback.from_user.id or not _is_admin(callback, ctx):
        return None
    state = await ctx.db.get_state(callback.from_user.id)
    if _panel_message_id(state) != callback.message.message_id:
        return None
    return state


async def _speech_targets(bot: Bot, ctx: AppContext, user_id: int, chat_id: int, panel_id: int, page: int = 0) -> None:
    targets = ctx.settings.channel_ids
    if not 0 <= page <= (len(targets) - 1) // 8:
        return
    rows = []
    if len(targets) > 1:
        rows.append([InlineKeyboardButton(text=f"全部已設定目標（{len(targets)} 個）", callback_data="admin:speak_to:all")])
    for target in targets[page * 8:(page + 1) * 8]:
        title = "群組／頻道"
        try:
            chat = await bot.get_chat(target, request_timeout=5)
            title = " ".join((chat.title or title).split())[:30]
        except (TelegramAPIError, TimeoutError, OSError):
            pass
        rows.append([InlineKeyboardButton(text=f"{title}（{target}）", callback_data=f"admin:speak_to:{target}")])
    navigation = page_buttons("admin:speak_page", page, (page + 1) * 8 < len(targets))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="取消／返回管理面板", callback_data="admin:back")])
    await update_admin_panel(
        bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
        text=f"直接發話：請選擇發布目標（第 {page + 1} 頁）。\n之後輸入文字，再選擇是否置頂。",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), state_name="admin_speech_targets",
    )


async def _send_speech(bot: Bot, ctx: AppContext, targets: list[int], text: str, pin: bool) -> list[str]:
    task = asyncio.current_task()
    if task:
        ctx.publish_tasks.add(task)
    results = []
    try:
        async with ctx.publish_lock:
            for target in targets:
                await _wait_for_send_slot(ctx, 1)
                try:
                    message = await bot.send_message(chat_id=target, text=text, parse_mode=None)
                except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                    logger.warning("admin speech rejected chat=%s error=%s", target, type(exc).__name__)
                    results.append(f"{target}：未發送，請檢查發話權限或稍後再試。")
                    continue
                except (TelegramAPIError, TimeoutError, OSError) as exc:
                    logger.warning("admin speech uncertain chat=%s error=%s", target, type(exc).__name__)
                    results.append(f"{target}：發送結果不明，請先到目標核對，勿直接重送。")
                    continue
                status = "已發送，未置頂"
                if pin:
                    try:
                        await bot.pin_chat_message(chat_id=target, message_id=message.message_id, disable_notification=True)
                        status = "已發送並置頂"
                    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                        logger.warning("admin speech pin rejected chat=%s error=%s", target, type(exc).__name__)
                        status = "已發送，但置頂失敗；請檢查置頂／編輯訊息權限，勿重送原文"
                    except (TelegramAPIError, TimeoutError, OSError) as exc:
                        logger.warning("admin speech pin uncertain chat=%s error=%s", target, type(exc).__name__)
                        status = "已發送，置頂結果不明；請核對置頂清單，勿重送原文"
                location = message_link(target, message.message_id) or f"訊息 ID：{message.message_id}"
                results.append(f"{target}：{status}\n{location}")
        return results
    finally:
        if task:
            ctx.publish_tasks.discard(task)


async def update_report_panel(bot: Bot, ctx: AppContext, user_id: int, chat_id: int, panel_id: int | None,
                              report_id: int, notice: str = "") -> None:
    report = await ctx.db.get_report(report_id)
    await update_admin_panel(
        bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
        text=(notice + "\n\n" if notice else "") + (format_report(report) if report else "找不到這筆檢舉。"),
        reply_markup=admin_report_keyboard(report) if report else admin_back_keyboard(),
        state_name="admin_report_detail", state_payload={"report_id": report_id},
    )


async def _send_report_reply(bot: Bot, ctx: AppContext, report: dict, text: str) -> str:
    task = asyncio.current_task()
    if task:
        ctx.publish_tasks.add(task)
    status, message_id = "uncertain", None
    try:
        try:
            message = await bot.send_message(chat_id=report["reporter_user_id"], text=format_report_reply(report, text), parse_mode=None)
            status, message_id = "sent", message.message_id
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            status = "failed"
            logger.warning("report reply rejected report=%s error=%s", report["report_id"], type(exc).__name__)
        except (TelegramAPIError, TimeoutError, OSError) as exc:
            logger.warning("report reply uncertain report=%s error=%s", report["report_id"], type(exc).__name__)
        return status
    finally:
        try:
            await ctx.db.finish_report_reply(report["report_id"], status, message_id)
        finally:
            if task:
                ctx.publish_tasks.discard(task)


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
        await callback.answer("這個管理面板已失效，請重新輸入 /admin。", show_alert=True)
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

        if action in {"report", "report_reply", "report_close", "reply_edit", "reply_send", "report_close_yes"}:
            expected_length = 4 if action in {"reply_edit", "reply_send", "report_close_yes"} else 3
            if len(parts) != expected_length or not parts[2].isdigit() or len(parts[2]) > 18 or int(parts[2]) <= 0:
                return
            report_id = int(parts[2])
            report = await ctx.db.get_report(report_id)
            if not report:
                await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id)
                return
            if action == "report":
                await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id)
                return
            payload = state["payload"]
            if expected_length == 4:
                expected_state = "admin_report_close_confirm" if action == "report_close_yes" else "admin_report_reply_confirm"
                if state["state"] != expected_state or payload.get("report_id") != report_id or payload.get("nonce") != parts[3]:
                    return
            if report["status"] != "open":
                await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id, "這筆檢舉已結案，未送出任何新回覆。")
                return
            if action == "report_close":
                nonce = secrets.token_urlsafe(12)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="確認結案（不另發送通知）", callback_data=f"admin:report_close_yes:{report_id}:{nonce}")],
                    [InlineKeyboardButton(text="返回檢舉／先回覆", callback_data=f"admin:report:{report_id}")],
                ])
                await update_admin_panel(
                    bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                    text=format_report(report) + "\n\n確定結案這筆檢舉嗎？只結束這一筆紀錄，不刪除貼文，也不另外通知檢舉人。",
                    reply_markup=keyboard, state_name="admin_report_close_confirm", state_payload={"report_id": report_id, "nonce": nonce},
                )
                return
            if action == "report_close_yes":
                async with ctx.article_locks[report["article_id"]]:
                    closed = await ctx.db.close_report(report_id)
                await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id,
                                          "已結案，未另外發送通知。" if closed else "這筆檢舉已結案或正在傳送回覆，請稍後確認。")
                return
            if report["reply_status"] not in {"none", "failed"}:
                await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id, "這筆檢舉已回覆或回覆結果待確認，不會重複傳送。")
                return
            if action in {"report_reply", "reply_edit"}:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="取消回覆／返回檢舉", callback_data=f"admin:report:{report_id}")],
                ])
                await update_admin_panel(
                    bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                    text=f"回覆檢舉 #{report_id} 的檢舉人（文章 #{report['article_id']}）。\n請輸入純文字回覆；送出後仍須另按結案。\n輸入 /cancel 可取消。",
                    reply_markup=keyboard, state_name="admin_wait_report_reply", state_payload={"report_id": report_id},
                )
                return
            async with ctx.article_locks[report["article_id"]]:
                # Claim the report in SQLite before the RPC so another administrator or a restart cannot resend it.
                await ctx.db.set_state(user_id, "admin_report_reply_sending", {"panel_message_id": panel_id, "report_id": report_id}, iso_utc())
                if await ctx.db.claim_report_reply(report_id):
                    result = await _send_report_reply(bot, ctx, report, payload["text"])
                    notice = {"sent": "已回覆檢舉人。檢舉尚未結案，請確認後按「結案」。",
                              "failed": "回覆未送出，對方可能已封鎖機器人或暫時無法收訊息；可重新回覆，或選擇不回覆結案。",
                              "uncertain": "回覆結果不明，不會自動重送；檢舉尚未結案，可確認處理結果後再結案。"}[result]
                else:
                    notice = "這筆檢舉已由其他管理員處理或回覆，不會重複傳送。"
            await update_report_panel(bot, ctx, user_id, chat_id, panel_id, report_id, notice)
            return

        if action == "speak" and len(parts) == 2:
            await _speech_targets(bot, ctx, user_id, chat_id, panel_id)
            return
        if action == "speak_page" and len(parts) == 3 and state["state"] == "admin_speech_targets":
            if parts[2].isdigit() and len(parts[2]) <= 6:
                await _speech_targets(bot, ctx, user_id, chat_id, panel_id, int(parts[2]))
            return
        if action == "speak_to" and len(parts) == 3 and state["state"] == "admin_speech_targets":
            if parts[2] == "all":
                targets = list(ctx.settings.channel_ids)
            else:
                try:
                    target = int(parts[2])
                except ValueError:
                    return
                if target not in ctx.settings.channel_ids:
                    return
                targets = [target]
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text=f"已選 {len(targets)} 個群組／頻道。\n請傳送要讓機器人發出的純文字。\n輸入 /cancel 可取消。",
                reply_markup=admin_back_keyboard(), state_name="admin_wait_speech",
                state_payload={"target_chat_ids": targets},
            )
            return
        if action in {"speak_send", "speak_edit"}:
            payload = state["payload"]
            expected_length = 4 if action == "speak_send" else 3
            if state["state"] != "admin_speech_confirm" or len(parts) != expected_length or parts[-1] != payload.get("nonce"):
                return
            targets = payload.get("target_chat_ids", [])
            if not targets or any(target not in ctx.settings.channel_ids for target in targets):
                await update_admin_panel(
                    bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                    text="目標設定已變更，請重新選擇直接發話。", reply_markup=admin_keyboard(),
                )
                return
            if action == "speak_edit":
                await update_admin_panel(
                    bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                    text="請重新傳送發話內容，輸入 /cancel 可取消。", reply_markup=admin_back_keyboard(),
                    state_name="admin_wait_speech", state_payload={"target_chat_ids": targets},
                )
                return
            if parts[2] not in {"0", "1"}:
                return
            # Consume confirmation before sending: a timeout or restart must not resend a possibly delivered message.
            await ctx.db.set_state(user_id, "admin_speech_sending", {"panel_message_id": panel_id}, iso_utc())
            results = await _send_speech(bot, ctx, targets, payload["text"], parts[2] == "1")
            for offset in range(0, len(results), 10):
                text = "直接發話結果\n\n" + "\n\n".join(results[offset:offset + 10])
                if offset + 10 < len(results):
                    await bot.send_message(chat_id=chat_id, text=text)
                else:
                    await update_admin_panel(
                        bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                        text=text, reply_markup=admin_keyboard(),
                    )
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
        if (action in {"issues", "reports"} and len(parts) == 3) or (action == "article_reports" and len(parts) == 4):
            page_part = parts[3] if action == "article_reports" else parts[2]
            article_filter = normalize_article_id(parts[2]) if action == "article_reports" else None
            if (action == "article_reports" and not article_filter) or not page_part.isdigit() or len(page_part) > 6:
                return
            page = int(page_part)
            size = 10 if action == "issues" else 3
            if action == "issues":
                rows = await ctx.db.list_attention_articles(size + 1, page * size)
            else:
                rows = await ctx.db.list_open_reports(size + 1, page * size, article_filter)
            lines = [f"{'發布／刪文' if action == 'issues' else '檢舉'}待處理（第 {page + 1} 頁）"]
            buttons = []
            for row in rows[:size]:
                article_id = row["article_id"]
                if action == "issues":
                    detail = "發布結果待核對" if row.get("publish_error_code") else "刪除失敗，需人工處理"
                else:
                    detail = f"檢舉 #{row['report_id']}：{row['reason']}"
                lines.append(f"\n#{article_id}\n{detail}")
                buttons.append([InlineKeyboardButton(text=f"處理 #{article_id}" if action == "issues" else f"處理檢舉 #{row['report_id']}",
                                                     callback_data=f"admin:review:{article_id}" if action == "issues" else f"admin:report:{row['report_id']}")])
            if not rows:
                lines.append("目前沒有待處理紀錄。")
            prefix = f"admin:article_reports:{article_filter}" if action == "article_reports" else f"admin:{action}"
            navigation = page_buttons(prefix, page, len(rows) > size)
            if navigation:
                buttons.append(navigation)
            buttons.append([InlineKeyboardButton(text="返回待處理事項", callback_data="admin:pending")])
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text="\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            return
        if action in {"review", "resolve"} and len(parts) == 3:
            article_id = normalize_article_id(parts[2])
            article = await ctx.db.get_article(article_id) if article_id else None
            if not article:
                return
            if action == "resolve":
                if not (article.get("publish_error_code") or article.get("delete_error_code")):
                    return
                nonce = secrets.token_urlsafe(12)
                text = (
                    f"請先到所有目標群組／頻道搜尋 #{article_id}。若仍有貼文，先手動刪除全部副本與圖片。\n\n"
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
            await update_admin_panel(
                bot, ctx, user_id=user_id, chat_id=chat_id, current_panel_id=panel_id,
                text=f"#{article_id} 的發布／刪文異常已依人工核對結果結案。檢舉仍保留待逐筆回覆及結案。", reply_markup=admin_back_keyboard(),
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
                text = f"#{article_id} 已刪除。檢舉仍保留待逐筆回覆及結案。"
            elif result.error_code == "publication_uncertain":
                text = "發布結果仍待核對，請先檢查所有目標，手動刪除副本後再人工結案。"
            elif result.error_code == "publication_in_progress":
                text = "文章仍在同步發布，請稍後再刪除。"
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
