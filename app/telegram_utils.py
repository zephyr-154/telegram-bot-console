from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


async def try_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramAPIError as exc:
        error_text = str(exc).casefold()
        if "message to delete not found" in error_text:
            return True
        logger.debug(
            "delete_message failed chat=%s message=%s error=%s",
            chat_id,
            message_id,
            type(exc).__name__,
        )
        return False
    except Exception as exc:
        # Cleanup must not hide the original publish/delete failure. The
        # article remains traceable in SQLite when this returns False.
        logger.warning(
            "unexpected delete_message failure chat=%s message=%s error=%s",
            chat_id,
            message_id,
            type(exc).__name__,
        )
        return False


async def delete_private_messages(
    bot: Bot,
    chat_id: int,
    message_ids: Iterable[int | None],
) -> None:
    unique_ids = dict.fromkeys(item for item in message_ids if item is not None)
    for message_id in unique_ids:
        await try_delete_message(bot, chat_id, int(message_id))
