from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Message, Update, User

from app.config import Settings
from app.chat_discovery import ChatRegistry, ChatTrackingMiddleware
from app.context import AppContext
from app.database import Database
from app.handlers import admin, private, user
from app.utils import utc_now


class ReplaySession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.serial = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("routing smoke must not access the network")
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append(method)
        if name in {"AnswerCallbackQuery", "DeleteMessage", "PinChatMessage"}:
            return True
        if name == "GetChat":
            return ChatFullInfo(
                id=int(method.chat_id), type="supergroup", title="測試群組", accent_color_id=0, max_reaction_count=0,
                accepted_gift_types={name: False for name in (
                    "unlimited_gifts", "limited_gifts", "unique_gifts", "premium_subscription", "gifts_from_channels",
                )},
            )
        assert name in {"SendMessage", "EditMessageText", "SendPhoto"}, name
        self.serial += 1
        return Message(
            message_id=getattr(method, "message_id", None) or self.serial,
            date=utc_now(), chat=Chat(id=int(method.chat_id), type="private" if int(method.chat_id) > 0 else "supergroup"),
            from_user=User(id=bot.id, is_bot=True, first_name="Test bot"),
            text=getattr(method, "text", None), caption=getattr(method, "caption", None),
        )


async def routing_smoke():
    with TemporaryDirectory(prefix="telegram-routing-") as directory:
        root = Path(directory)
        db = Database(root / "bot.sqlite3")
        await db.open()
        session = ReplaySession()
        bot = Bot("1234567890:test", session=session)
        settings = Settings("1234567890:test", -1001234, 99, db.path, root / "tmp", root / "font", root / "bold", None, "s" * 32)
        ctx = AppContext(settings, db)
        dispatcher = Dispatcher(ctx=ctx)
        registry = ChatRegistry(root, settings.bot_token)
        dispatcher.update.outer_middleware(ChatTrackingMiddleware(registry))
        dispatcher.include_routers(user.router, private.router, admin.router)
        update_id = 0

        async def incoming(text, actor=42, chat_id=None):
            nonlocal update_id
            update_id += 1
            target = actor if chat_id is None else chat_id
            message = Message(
                message_id=update_id, date=utc_now(), text=text,
                chat=Chat(id=target, type="private" if target > 0 else "supergroup"),
                from_user=User(id=actor, is_bot=False, first_name="Test user"),
            )
            await dispatcher.feed_update(bot, Update(update_id=update_id, message=message))

        async def click(data, actor, message_id):
            nonlocal update_id
            update_id += 1
            message = Message(message_id=message_id, date=utc_now(), chat=Chat(id=actor, type="private"))
            callback = CallbackQuery(id=str(update_id), from_user=User(id=actor, is_bot=False, first_name="Test user"), chat_instance="test", message=message, data=data)
            await dispatcher.feed_update(bot, Update(update_id=update_id, callback_query=callback))

        try:
            for command in ("/start", "/help", "/privacy", "/myposts", "/report", "/cancel", "/unknown"):
                await incoming(command)
                assert await db.get_pending_for_user(42) is None
            before = len(session.calls)
            await incoming("/admin")
            await incoming("群組內的文字不應成為投稿", chat_id=-1001234)
            assert len(session.calls) == before
            assert [chat["id"] for chat in registry.chats()] == [-1001234]
            assert "群組內的文字" not in registry.path.read_text(encoding="utf-8")
            with patch("app.services.publishing.render_post_images", return_value=[b"png"]):
                await incoming("正常投稿內容")
                pending = await db.get_pending_for_user(42)
                assert pending
                await click("submit:yes:" + pending["callback_nonce"], 42, pending["confirmation_message_id"])
            assert await db.get_pending_for_user(42) is None
            assert len([call for call in session.calls if type(call).__name__ == "SendPhoto"]) == 1
            assert len(await db.list_user_articles(42, 10, 0)) == 1
            await incoming("/admin", actor=99)
            panel_id = (await db.get_state(99))["payload"]["panel_message_id"]
            await click("admin:pause", 99, panel_id)
            await click("admin:set_pause:1", 99, panel_id)
            assert await db.submissions_paused()
            await click("admin:speak", 99, panel_id)
            await click("admin:speak_to:-1001234", 99, panel_id)
            await incoming("delete", actor=99)
            speech = await db.get_state(99)
            assert speech["state"] == "admin_speech_confirm"
            assert await db.get_pending_for_user(99) is None
            confirmation = "admin:speak_send:1:" + speech["payload"]["nonce"]
            await click(confirmation, 99, panel_id)
            await click(confirmation, 99, panel_id)
            sent = [call for call in session.calls if type(call).__name__ == "SendMessage" and call.chat_id == -1001234]
            assert len(sent) == 1 and sent[0].text == "delete" and sent[0].parse_mode is None
            assert len([call for call in session.calls if type(call).__name__ == "PinChatMessage"]) == 1
            await click("admin:speak", 99, panel_id)
            await click("admin:speak_to:all", 99, panel_id)
            await incoming("/cancel", actor=99)
            assert await db.get_state(99) is None
            article_id = (await db.list_user_articles(42, 10, 0))[0]["article_id"]
            await incoming("/report", actor=43)
            assert (await db.get_state(43))["state"] == "report_wait_id"
            await incoming(article_id, actor=43)
            assert (await db.get_state(43))["state"] == "report_wait_reason"
            await incoming("檢舉原因", actor=43)
            assert await db.get_state(43) is None
            assert await db.get_pending_for_user(43) is None
            report_id = (await db.list_open_reports(10, 0))[0]["report_id"]
            await incoming("/admin", actor=99)
            panel_id = (await db.get_state(99))["payload"]["panel_message_id"]
            await click("admin:reports:0", 99, panel_id)
            await click(f"admin:report:{report_id}", 99, panel_id)
            await click(f"admin:report_reply:{report_id}", 99, panel_id)
            await incoming("已處理檢舉", actor=99)
            reply = await db.get_state(99)
            data = f"admin:reply_send:{report_id}:" + reply["payload"]["nonce"]
            await click(data, 99, panel_id)
            await click(data, 99, panel_id)
            assert (await db.get_report(report_id))["status"] == "open"
            replies = [call for call in session.calls if type(call).__name__ == "SendMessage" and call.chat_id == 43 and "管理員回覆" in call.text]
            assert len(replies) == 1 and "已處理檢舉" in replies[0].text
            await click(f"admin:report_close:{report_id}", 99, panel_id)
            closing = await db.get_state(99)
            await click(f"admin:report_close_yes:{report_id}:" + closing["payload"]["nonce"], 99, panel_id)
            assert (await db.get_report(report_id))["status"] == "closed"
        finally:
            await db.close()
            await bot.session.close()


def test_real_dispatcher_routes_commands_and_callbacks_without_network():
    command = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", command, str(Path(__file__).resolve())],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    asyncio.run(routing_smoke())
