from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import PinChatMessage, SendMessage

from app.handlers.admin import admin_callback
from app.handlers.private import admin_command, private_text
from app.handlers.user import cancel_command
from conftest import FakeCallback


async def click(ctx, bot, data, user_id=99, panel_id=None):
    if panel_id is None:
        panel_id = (await ctx.db.get_state(user_id))["payload"]["panel_message_id"]
    await admin_callback(FakeCallback(bot, data, user_id=user_id, message_id=panel_id), bot, ctx)


async def draft(ctx, bot, *, target="all", text="公告內容\n第二行", user_id=99):
    await admin_command(bot.message(user_id=user_id, text="/admin"), bot, ctx)
    await click(ctx, bot, "admin:speak", user_id)
    await click(ctx, bot, "admin:speak_to:" + target, user_id)
    await private_text(bot.message(user_id=user_id, text=text), bot, ctx)
    return await ctx.db.get_state(user_id)


@pytest.fixture(autouse=True)
def immediate_send_slot(monkeypatch):
    async def slot(*args):
        pass
    monkeypatch.setattr("app.handlers.admin._wait_for_send_slot", slot)


@pytest.mark.parametrize("pin", [False, True])
def test_speech_fans_out_as_plain_text_and_only_pins_when_selected(ctx, bot, pin):
    ctx.settings = replace(ctx.settings, additional_channel_ids=(-1002000,))

    async def scenario():
        await ctx.db.set_submissions_paused(True)
        speech = await draft(ctx, bot, text="<b>原文</b>\n公告")
        assert speech["state"] == "admin_speech_confirm"
        assert not [message for message in bot.messages if message["chat_id"] < 0]
        await click(ctx, bot, f"admin:speak_send:{int(pin)}:" + speech["payload"]["nonce"])
        sent = [message for message in bot.messages if message["chat_id"] < 0]
        assert [message["chat_id"] for message in sent] == list(ctx.settings.channel_ids)
        assert all(message["text"] == "<b>原文</b>\n公告" and message["parse_mode"] is None for message in sent)
        assert len(bot.pinned) == (2 if pin else 0)
        assert all(message["disable_notification"] for message in bot.pinned)
        assert not bot.photo_calls and not bot.media_calls and not ctx.publish_tasks
        assert await ctx.db.get_pending_for_user(99) is None
        assert not await ctx.db.list_user_articles(99, 10, 0)
        final_state = await ctx.db.get_state(99)
        assert final_state["state"] == "admin_panel" and "text" not in final_state["payload"]
    asyncio.run(scenario())


def test_speech_rejects_foreign_old_and_replayed_confirmations(ctx, bot):
    async def scenario():
        speech = await draft(ctx, bot)
        panel_id = speech["payload"]["panel_message_id"]
        data = "admin:speak_send:0:" + speech["payload"]["nonce"]
        await click(ctx, bot, data, user_id=42, panel_id=panel_id)
        await click(ctx, bot, data, panel_id=panel_id - 1)
        await click(ctx, bot, "admin:speak_send:0:wrong")
        assert not [message for message in bot.messages if message["chat_id"] < 0]
        await asyncio.gather(click(ctx, bot, data), click(ctx, bot, data))
        await click(ctx, bot, data)
        assert len([message for message in bot.messages if message["chat_id"] < 0]) == 1
    asyncio.run(scenario())


def test_each_admin_can_choose_a_different_target_without_sharing_drafts(ctx, bot):
    ctx.settings = replace(ctx.settings, additional_channel_ids=(-1002000,), additional_admin_user_ids=(100,))

    async def scenario():
        first = await draft(ctx, bot, target="-1001234", text="第一位的公告")
        second = await draft(ctx, bot, target="-1002000", text="第二位的公告", user_id=100)
        forged = FakeCallback(bot, "admin:speak_send:0:" + first["payload"]["nonce"], user_id=100,
                              message_id=first["payload"]["panel_message_id"])
        forged.message.chat.id = 99
        await admin_callback(forged, bot, ctx)
        await click(ctx, bot, "admin:speak_send:0:" + second["payload"]["nonce"], user_id=100)
        await click(ctx, bot, "admin:speak_send:0:" + first["payload"]["nonce"])
        assert [(message["chat_id"], message["text"]) for message in bot.messages if message["chat_id"] < 0] == [
            (-1002000, "第二位的公告"), (-1001234, "第一位的公告"),
        ]
    asyncio.run(scenario())


def test_pin_failure_keeps_sent_message_and_continues_other_targets(ctx, bot, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_channel_ids=(-1002000,))
    original_pin = bot.pin_chat_message

    async def pin(**kwargs):
        if kwargs["chat_id"] == -1001234:
            raise TelegramBadRequest(method=PinChatMessage(**kwargs), message="not enough rights")
        return await original_pin(**kwargs)
    monkeypatch.setattr(bot, "pin_chat_message", pin)

    async def scenario():
        speech = await draft(ctx, bot)
        deleted_before = list(bot.deleted)
        data = "admin:speak_send:1:" + speech["payload"]["nonce"]
        await click(ctx, bot, data)
        await click(ctx, bot, data)
        assert len([message for message in bot.messages if message["chat_id"] < 0]) == 2
        assert len(bot.pinned) == 1 and bot.deleted == deleted_before
        result = bot.edits[-1]["text"]
        assert "已發送，但置頂失敗" in result and "已發送並置頂" in result
        assert "https://t.me/c/1234/" in result
    asyncio.run(scenario())


def test_partial_send_failure_and_unknown_result_are_reported_without_retries(ctx, bot, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_channel_ids=(-1002000, -1003000))
    original_send = bot.send_message
    attempted = []

    async def send(**kwargs):
        target = kwargs["chat_id"]
        if target < 0:
            attempted.append(target)
            state = await ctx.db.get_state(99)
            assert state["state"] == "admin_speech_sending"
            assert "text" not in state["payload"] and "nonce" not in state["payload"]
        if target == -1001234:
            raise TelegramForbiddenError(method=SendMessage(**kwargs), message="bot was kicked")
        if target == -1002000:
            raise TimeoutError("unknown result")
        return await original_send(**kwargs)
    monkeypatch.setattr(bot, "send_message", send)

    async def scenario():
        speech = await draft(ctx, bot)
        data = "admin:speak_send:0:" + speech["payload"]["nonce"]
        await click(ctx, bot, data)
        await click(ctx, bot, data)
        assert attempted == [-1001234, -1002000, -1003000]
        result = bot.edits[-1]["text"]
        assert "未發送" in result and "發送結果不明" in result and "已發送，未置頂" in result
        assert not ctx.publish_tasks
    asyncio.run(scenario())


def test_edit_and_cancel_invalidate_old_confirmation(ctx, bot):
    async def scenario():
        old = await draft(ctx, bot)
        old_nonce = old["payload"]["nonce"]
        await click(ctx, bot, "admin:speak_edit:" + old_nonce)
        await private_text(bot.message(user_id=99, text="修改後內容"), bot, ctx)
        current = await ctx.db.get_state(99)
        assert current["payload"]["nonce"] != old_nonce
        await click(ctx, bot, "admin:speak_send:0:" + old_nonce)
        assert not [message for message in bot.messages if message["chat_id"] < 0]
        await cancel_command(bot.message(user_id=99, text="/cancel"), bot, ctx)
        assert await ctx.db.get_state(99) is None
        await click(ctx, bot, "admin:speak_send:0:" + current["payload"]["nonce"], panel_id=current["payload"]["panel_message_id"])
        assert not [message for message in bot.messages if message["chat_id"] < 0]
    asyncio.run(scenario())


@pytest.mark.parametrize("text,valid", [(" \n ", False), ("a" * 4097, False), ("😀" * 2049, False),
                                      ("a" * 4096, True), ("😀" * 2048, True), ("/announcement 測試", True)])
def test_speech_checks_text_length_and_accepts_plain_slash_text(ctx, bot, text, valid):
    async def scenario():
        speech = await draft(ctx, bot, text=text)
        assert speech["state"] == ("admin_speech_confirm" if valid else "admin_wait_speech")
        if valid:
            await click(ctx, bot, "admin:speak_send:0:" + speech["payload"]["nonce"])
            assert [message["text"] for message in bot.messages if message["chat_id"] < 0] == [text]
        else:
            assert "4096" in bot.messages[-1]["text"]
        assert all(len(edit["text"].encode("utf-16-le")) // 2 <= 4096 for edit in bot.edits)
    asyncio.run(scenario())


def test_unconfigured_and_removed_targets_are_never_sent_to(ctx, bot):
    async def scenario():
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await click(ctx, bot, "admin:speak")
        await click(ctx, bot, "admin:speak_to:-1009999")
        assert (await ctx.db.get_state(99))["state"] == "admin_speech_targets"
        speech = await draft(ctx, bot)
        ctx.settings = replace(ctx.settings, channel_id=-1009999)
        await click(ctx, bot, "admin:speak_send:0:" + speech["payload"]["nonce"])
        assert not [message for message in bot.messages if message["chat_id"] < 0]
        assert "目標設定已變更" in bot.edits[-1]["text"]
    asyncio.run(scenario())


def test_target_menu_pages_and_keeps_ids_when_name_lookup_fails(ctx, bot, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_channel_ids=tuple(-1002000 - index for index in range(9)))

    async def get_chat(target, **kwargs):
        raise TelegramForbiddenError(method=SendMessage(chat_id=target, text="unused"), message="no access")
    monkeypatch.setattr(bot, "get_chat", get_chat)

    async def scenario():
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await click(ctx, bot, "admin:speak")
        await click(ctx, bot, "admin:speak_page:1")
        buttons = bot.edits[-1]["reply_markup"].inline_keyboard
        assert any(str(ctx.settings.channel_ids[-1]) in button.text for row in buttons for button in row)
        assert any(button.callback_data == "admin:speak_page:0" for row in buttons for button in row)
        await click(ctx, bot, "admin:speak_to:" + str(ctx.settings.channel_ids[-1]))
        assert (await ctx.db.get_state(99))["payload"]["target_chat_ids"] == [ctx.settings.channel_ids[-1]]
    asyncio.run(scenario())


def test_interrupted_send_consumes_confirmation_and_releases_task_tracking(ctx, bot, monkeypatch):
    original_send = bot.send_message

    async def send(**kwargs):
        if kwargs["chat_id"] < 0:
            raise asyncio.CancelledError()
        return await original_send(**kwargs)
    monkeypatch.setattr(bot, "send_message", send)

    async def scenario():
        speech = await draft(ctx, bot)
        data = "admin:speak_send:0:" + speech["payload"]["nonce"]
        with pytest.raises(asyncio.CancelledError):
            await click(ctx, bot, data)
        assert not ctx.publish_tasks
        assert (await ctx.db.get_state(99))["state"] == "admin_speech_sending"
        await click(ctx, bot, data)
        assert not [message for message in bot.messages if message["chat_id"] < 0]
    asyncio.run(scenario())


def test_pin_timeout_reports_sent_message_without_claiming_pin_success(ctx, bot, monkeypatch):
    async def pin(**kwargs):
        raise TimeoutError("unknown pin result")
    monkeypatch.setattr(bot, "pin_chat_message", pin)

    async def scenario():
        speech = await draft(ctx, bot)
        await click(ctx, bot, "admin:speak_send:1:" + speech["payload"]["nonce"])
        assert len([message for message in bot.messages if message["chat_id"] < 0]) == 1
        assert "已發送，置頂結果不明" in bot.edits[-1]["text"]
        assert "已發送並置頂" not in bot.edits[-1]["text"]
    asyncio.run(scenario())


def test_many_target_results_are_reported_in_bounded_messages(ctx, bot):
    ctx.settings = replace(ctx.settings, additional_channel_ids=tuple(-1002000 - index for index in range(20)))

    async def scenario():
        speech = await draft(ctx, bot)
        await click(ctx, bot, "admin:speak_send:0:" + speech["payload"]["nonce"])
        assert len([message for message in bot.messages if message["chat_id"] < 0]) == 21
        reports = [message["text"] for message in bot.messages if message["chat_id"] == 99 and message["text"].startswith("直接發話結果")]
        reports.append(bot.edits[-1]["text"])
        assert len(reports) == 3
        assert all(len(report) <= 4096 for report in reports)
        for target in ctx.settings.channel_ids:
            assert sum(f"{target}：" in report for report in reports) == 1
    asyncio.run(scenario())
