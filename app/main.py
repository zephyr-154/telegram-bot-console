from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from app.backup import database_lock
from app.config import ConfigurationError, default_env_path, load_settings
from app.chat_discovery import ChatRegistry, ChatTrackingMiddleware, DISCOVERY_UPDATES
from app.context import AppContext
from app.database import Database
from app.handlers import admin, private, user
from app.services.admin_badges import watch_admin_badges
from app.telegram_utils import delete_private_messages
from app.utils import PENDING_TTL, iso_utc, utc_now


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _cleanup_stale_data(ctx: AppContext, bot: Bot) -> None:
    cutoff = iso_utc(utc_now() - PENDING_TTL)
    stale_pending = await ctx.db.pending_before(cutoff)
    for pending in stale_pending:
        user_id = int(pending["user_id"])
        if ctx.user_locks[user_id].locked():
            continue
        async with ctx.user_locks[user_id]:
            current = await ctx.db.get_pending_by_nonce(pending["callback_nonce"])
            if current:
                await ctx.db.delete_pending(user_id)
                await delete_private_messages(
                    bot, int(pending["source_chat_id"]),
                    [pending.get("source_message_id"), pending.get("confirmation_message_id")],
                )
    await ctx.db.purge_states_before(cutoff)


async def _maintenance(ctx: AppContext, bot: Bot) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await _cleanup_stale_data(ctx, bot)
        except Exception as exc:
            logging.getLogger(__name__).warning("periodic cleanup failed error=%s", type(exc).__name__)


async def _validate_destination(ctx: AppContext, bot: Bot) -> None:
    for channel_id in ctx.settings.channel_ids:
        try:
            await _validate_target(bot, channel_id)
        except ConfigurationError as exc:
            raise ConfigurationError(f"目標 {channel_id}：{exc}") from None


async def _validate_target(bot: Bot, channel_id: int) -> None:
    chat = await bot.get_chat(channel_id)
    if chat.type not in {"channel", "group", "supergroup"}:
        raise ConfigurationError("投稿目標必須是群組、超級群組或頻道。")
    me = await bot.get_me()
    membership = await bot.get_chat_member(channel_id, me.id)
    if membership.status in {"left", "kicked"}:
        raise ConfigurationError("Bot 尚未加入投稿目標或已被移除。")
    if chat.type == "channel" and membership.status != "creator" and not getattr(membership, "can_post_messages", False):
        raise ConfigurationError("Bot 缺少頻道發文權限。")
    if membership.status == "restricted" and not getattr(membership, "can_send_photos", False):
        raise ConfigurationError("Bot 在目標群組無法傳送圖片。")
    permissions = getattr(chat, "permissions", None)
    if membership.status == "member" and permissions and permissions.can_send_photos is False:
        raise ConfigurationError("目標群組的預設權限禁止傳送圖片，請調整權限或將 Bot 設為管理員。")


async def run(env_path: str | Path | None = None, *, ready_callback: Callable[[], None] | None = None, use_environment: bool = True) -> None:
    if os.name != "nt":
        os.umask(0o077)
    settings = load_settings(env_path, use_environment=use_environment)
    settings.prepare_directories()
    settings.validate_runtime()

    db = Database(settings.database_path)
    bot = Bot(token=settings.bot_token)
    try:
        with database_lock(settings.database_path):
            ctx = AppContext(settings=settings, db=db)
            maintenance = None
            badges = None
            try:
                await db.open()
                await db.discard_incomplete_articles()
                await _cleanup_stale_data(ctx, bot)
                await _validate_destination(ctx, bot)
                await bot.set_my_commands([
                    BotCommand(command="start", description="開始投稿"),
                    BotCommand(command="myposts", description="我的投稿與刪文"),
                    BotCommand(command="report", description="檢舉文章"),
                    BotCommand(command="privacy", description="匿名與資料保存說明"),
                    BotCommand(command="cancel", description="取消目前操作"),
                    BotCommand(command="help", description="操作說明"),
                ])
                dispatcher = Dispatcher(ctx=ctx)
                registry = ChatRegistry(Path(env_path or default_env_path()).expanduser().resolve().parent, settings.bot_token)
                dispatcher.update.outer_middleware(ChatTrackingMiddleware(registry))
                dispatcher.include_router(user.router)
                dispatcher.include_router(private.router)
                dispatcher.include_router(admin.router)
                maintenance = asyncio.create_task(_maintenance(ctx, bot))
                badges = asyncio.create_task(watch_admin_badges(ctx, bot))
                if ready_callback is not None:
                    ready_callback()
                await dispatcher.start_polling(bot, allowed_updates=sorted(set(dispatcher.resolve_used_update_types()) | DISCOVERY_UPDATES))
            finally:
                if badges:
                    badges.cancel()
                    with suppress(asyncio.CancelledError):
                        await badges
                if maintenance:
                    maintenance.cancel()
                    with suppress(asyncio.CancelledError):
                        await maintenance
                publishing = list(ctx.publish_tasks)
                for task in publishing:
                    task.cancel()
                await asyncio.gather(*publishing, return_exceptions=True)
                await db.close()
    finally:
        await bot.session.close()


def main(env_path: str | Path | None = None) -> None:
    configure_logging()
    try:
        asyncio.run(run(env_path))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot 已停止")


if __name__ == "__main__":
    main()
