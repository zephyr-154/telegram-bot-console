from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.context import AppContext
from app.handlers.private import clear_user_operation, prompt_article_deletion
from app.keyboards import home_keyboard, page_buttons
from app.messages import PRIVACY_TEXT, WELCOME_TEXT
from app.services.reporting import submit_report
from app.utils import article_status_text, iso_utc, message_link, normalize_article_id

router = Router(name="user_commands")
REPORT_HELP = "私訊輸入 /report，再依提示填寫文章 ID 和檢舉原因。\n原因限 1 至 500 字，每人 10 分鐘最多 3 件；相同文章只能檢舉一次。輸入 /cancel 可取消。"


@router.message(Command("help", "privacy"), F.chat.type == ChatType.PRIVATE)
async def help_command(message: Message) -> None:
    command = (message.text or "").split()[0].split("@", 1)[0].casefold()
    await message.answer(PRIVACY_TEXT if command == "/privacy" else WELCOME_TEXT, reply_markup=home_keyboard())


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cancel_command(message: Message, bot: Bot, ctx: AppContext) -> None:
    user_id = message.from_user.id
    if ctx.user_locks[user_id].locked():
        await message.answer("已確認的投稿或操作正在處理，無法中途取消，請稍候。")
        return
    async with ctx.user_locks[user_id]:
        await clear_user_operation(message, bot, ctx, user_id)
        await message.answer("已取消目前操作。", reply_markup=home_keyboard())


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("help:"))
async def help_callback(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.id != callback.from_user.id:
        await callback.answer("請在自己的私訊中操作。", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        PRIVACY_TEXT if callback.data == "help:privacy" else REPORT_HELP,
        reply_markup=home_keyboard(),
    )


@router.message(Command("report"), F.chat.type == ChatType.PRIVATE)
async def report_command(message: Message, bot: Bot, ctx: AppContext) -> None:
    parts = (message.text or "").split(maxsplit=2)
    user_id = message.from_user.id
    if ctx.user_locks[user_id].locked():
        await message.answer("目前有操作正在處理，請稍後再檢舉。")
        return
    async with ctx.user_locks[user_id]:
        if len(parts) != 3:
            await clear_user_operation(message, bot, ctx, user_id)
            await ctx.db.set_state(user_id, "report_wait_id", {}, iso_utc())
            await message.answer("請輸入要檢舉的文章 ID，例如 XG_ABCDEFGH。輸入 /cancel 可取消。")
            return
        article_id, reason = normalize_article_id(parts[1]), parts[2].strip()
        if not article_id or not reason or len(reason) > 500:
            await message.answer("請填寫正確的文章 ID，檢舉原因需為 1 至 500 字。")
            return
        await clear_user_operation(message, bot, ctx, user_id)
        await submit_report(message, bot, ctx, article_id, reason)


@router.message(Command("myposts"), F.chat.type == ChatType.PRIVATE)
async def my_posts_command(message: Message, ctx: AppContext) -> None:
    await _show_posts(message, ctx, message.from_user.id, 0, edit=False)


async def _reply_or_edit(message: Message, text: str, keyboard: InlineKeyboardMarkup, edit: bool) -> None:
    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).casefold():
                return
    await message.answer(text, reply_markup=keyboard)


async def _show_posts(message: Message, ctx: AppContext, user_id: int, page: int, *, edit: bool) -> None:
    rows = await ctx.db.list_user_articles(user_id, 6, page * 5)
    visible, has_next = rows[:5], len(rows) > 5
    lines = [f"我的投稿（第 {page + 1} 頁）"]
    buttons = []
    for article in visible:
        article_id = article["article_id"]
        lines.append(f"\n#{article_id}\n{article_status_text(article, ctx.settings.timezone)}")
        buttons.append([InlineKeyboardButton(text=f"查看 #{article_id}", callback_data=f"mine:open:{article_id}")])
    if not visible:
        lines.append("目前沒有投稿，直接傳送純文字即可。" if page == 0 else "此頁沒有投稿，請返回上一頁。")
    navigation = page_buttons("mine:page", page, has_next)
    if navigation:
        buttons.append(navigation)
    if not buttons:
        buttons = home_keyboard().inline_keyboard
    await _reply_or_edit(message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons), edit)


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("mine:"))
async def my_posts_callback(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not callback.message or callback.message.chat.id != callback.from_user.id:
        await callback.answer("請在自己的私訊中操作。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("操作已失效。", show_alert=True)
        return
    user_id = callback.from_user.id
    if parts[1] == "page":
        if not parts[2].isdigit() or len(parts[2]) > 6:
            await callback.answer("頁碼不正確。", show_alert=True)
            return
        await callback.answer()
        await _show_posts(callback.message, ctx, user_id, int(parts[2]), edit=True)
        return
    article_id = normalize_article_id(parts[2])
    article = await ctx.db.get_article(article_id) if article_id else None
    if not article or article["author_user_id"] != user_id:
        await callback.answer("只能查看或刪除自己的投稿。", show_alert=True)
        return
    if parts[1] == "delete":
        if ctx.user_locks[user_id].locked():
            await callback.answer("目前有操作正在處理，請稍候。", show_alert=True)
            return
        await callback.answer()
        async with ctx.user_locks[user_id]:
            await clear_user_operation(callback.message, bot, ctx, user_id)
            await prompt_article_deletion(callback.message, bot, ctx, {}, article_id, user_id)
        return
    if parts[1] != "open":
        await callback.answer("操作已失效。", show_alert=True)
        return
    await callback.answer()
    rows = await ctx.db.get_article_messages(article_id)
    buttons = []
    link = None
    if rows and article["status"] != "deleted":
        seen_targets = set()
        for row in rows:
            if row["channel_id"] in seen_targets:
                continue
            seen_targets.add(row["channel_id"])
            target_link = message_link(row["channel_id"], row["message_id"])
            if target_link:
                link = target_link
                buttons.append([InlineKeyboardButton(text=f"開啟貼文 · {row['channel_id']}", url=target_link)])
        buttons.append([InlineKeyboardButton(text="申請刪除此文", callback_data=f"mine:delete:{article_id}")])
    buttons.append([InlineKeyboardButton(text="返回我的投稿", callback_data="mine:page:0")])
    await _reply_or_edit(
        callback.message,
        f"#{article_id}\n{article_status_text(article, ctx.settings.timezone)}\n"
        + ("貼文連結僅限有權限查看原群組／頻道的帳號開啟。" if link else "目前無可用貼文連結，可使用文章 ID 申請刪文。")
        + "超過自助刪文期限請聯絡管理員。",
        InlineKeyboardMarkup(inline_keyboard=buttons), True,
    )
