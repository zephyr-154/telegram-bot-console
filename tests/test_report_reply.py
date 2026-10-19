from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.handlers.admin import admin_callback
from app.handlers.private import admin_command, private_text
from app.handlers.user import cancel_command
from app.utils import iso_utc, utc_now
from conftest import FakeCallback


ARTICLE = "XG_ABCDEFGH"


async def seed(ctx, reporters=(43, 44)):
    await ctx.db.create_article(ARTICLE, 42, "digest")
    for reporter in reporters:
        await ctx.db.create_report(ARTICLE, reporter, f"檢舉原因 {reporter}", iso_utc(), iso_utc(utc_now() - timedelta(minutes=10)))
    return {row["reporter_user_id"]: row["report_id"] for row in await ctx.db.list_open_reports(100, 0)}


async def click(ctx, bot, data, user_id=99, panel_id=None):
    if panel_id is None:
        panel_id = (await ctx.db.get_state(user_id))["payload"]["panel_message_id"]
    await admin_callback(FakeCallback(bot, data, user_id=user_id, message_id=panel_id), bot, ctx)


async def draft(ctx, bot, report_id, text="已處理，感謝你的回報。", user_id=99):
    await admin_command(bot.message(user_id=user_id, text="/admin"), bot, ctx)
    await click(ctx, bot, f"admin:report:{report_id}", user_id)
    await click(ctx, bot, f"admin:report_reply:{report_id}", user_id)
    await private_text(bot.message(user_id=user_id, text=text), bot, ctx)
    return await ctx.db.get_state(user_id)


async def close(ctx, bot, report_id):
    await click(ctx, bot, f"admin:report_close:{report_id}")
    assert (await ctx.db.get_report(report_id))["status"] == "open"
    state = await ctx.db.get_state(99)
    await click(ctx, bot, f"admin:report_close_yes:{report_id}:" + state["payload"]["nonce"])


def test_reply_only_reaches_selected_reporter_and_requires_separate_close(ctx, bot):
    async def scenario():
        reports = await seed(ctx)
        report_id = reports[43]
        state = await draft(ctx, bot, report_id, text="<b>處理完成</b>\n謝謝")
        assert state["state"] == "admin_report_reply_confirm"
        assert not [message for message in bot.messages if message["chat_id"] in {42, 43, 44}]
        data = f"admin:reply_send:{report_id}:" + state["payload"]["nonce"]
        await click(ctx, bot, data)
        await click(ctx, bot, data)
        replies = [message for message in bot.messages if message["chat_id"] in {42, 43, 44}]
        assert len(replies) == 1 and replies[0]["chat_id"] == 43 and replies[0]["parse_mode"] is None
        assert ARTICLE in replies[0]["text"] and replies[0]["text"].endswith("<b>處理完成</b>\n謝謝")
        report = await ctx.db.get_report(report_id)
        assert report["status"] == "open" and report["reply_status"] == "sent" and report["reply_message_id"]
        assert "text" not in (await ctx.db.get_state(99))["payload"]
        assert "尚未結案" in bot.edits[-1]["text"]
        await close(ctx, bot, report_id)
        assert (await ctx.db.get_report(report_id))["status"] == "closed"
        assert (await ctx.db.get_report(reports[44]))["status"] == "open"
        assert (await ctx.db.get_article(ARTICLE))["status"] == "publishing"
        assert len([message for message in bot.messages if message["chat_id"] == 43]) == 1
    asyncio.run(scenario())


def test_close_without_reply_is_confirmed_and_does_not_notify_anyone(ctx, bot):
    async def scenario():
        report_id = (await seed(ctx))[43]
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await click(ctx, bot, f"admin:report:{report_id}")
        await close(ctx, bot, report_id)
        assert (await ctx.db.get_report(report_id))["reply_status"] == "none"
        assert not [message for message in bot.messages if message["chat_id"] != 99]
    asyncio.run(scenario())


def test_two_admins_replying_to_same_report_send_only_once(ctx, bot):
    ctx.settings = replace(ctx.settings, additional_admin_user_ids=(100,))

    async def scenario():
        report_id = (await seed(ctx))[43]
        first = await draft(ctx, bot, report_id, user_id=99)
        second = await draft(ctx, bot, report_id, user_id=100)
        await asyncio.gather(
            click(ctx, bot, f"admin:reply_send:{report_id}:" + first["payload"]["nonce"], user_id=99),
            click(ctx, bot, f"admin:reply_send:{report_id}:" + second["payload"]["nonce"], user_id=100),
        )
        assert len([message for message in bot.messages if message["chat_id"] == 43]) == 1
        assert (await ctx.db.get_report(report_id))["status"] == "open"
        assert not ctx.publish_tasks
        actions = await ctx.db.list_report_actions(report_id, 10, 0)
        assert len(actions) == 1 and actions[0]["status"] == "sent"
        assert actions[0]["admin_user_id"] in {99, 100}
    asyncio.run(scenario())


def test_foreign_report_nonce_old_panel_and_non_admin_cannot_send(ctx, bot):
    async def scenario():
        reports = await seed(ctx)
        report_id = reports[43]
        state = await draft(ctx, bot, report_id)
        data = f"admin:reply_send:{report_id}:" + state["payload"]["nonce"]
        panel_id = state["payload"]["panel_message_id"]
        await click(ctx, bot, data, user_id=42, panel_id=panel_id)
        await click(ctx, bot, data, panel_id=panel_id - 1)
        await click(ctx, bot, f"admin:reply_send:{reports[44]}:" + state["payload"]["nonce"])
        await click(ctx, bot, f"admin:reply_send:{report_id}:wrong")
        assert not [message for message in bot.messages if message["chat_id"] != 99]
        assert (await ctx.db.get_report(report_id))["reply_status"] == "none"
    asyncio.run(scenario())


@pytest.mark.parametrize("error,expected", [("blocked", "failed"), ("timeout", "uncertain")])
def test_reply_failure_keeps_case_open_and_does_not_automatically_retry(ctx, bot, monkeypatch, error, expected):
    original_send = bot.send_message
    attempted = []

    async def send(**kwargs):
        if kwargs["chat_id"] == 43:
            attempted.append(kwargs)
            report = await ctx.db.get_report(report_id)
            assert report["reply_status"] == "sending"
            assert "text" not in (await ctx.db.get_state(99))["payload"]
            if error == "blocked":
                raise TelegramForbiddenError(method=SendMessage(**kwargs), message="blocked by user")
            raise TimeoutError("unknown send result")
        return await original_send(**kwargs)
    monkeypatch.setattr(bot, "send_message", send)

    async def scenario():
        nonlocal report_id
        report_id = (await seed(ctx))[43]
        state = await draft(ctx, bot, report_id)
        data = f"admin:reply_send:{report_id}:" + state["payload"]["nonce"]
        await click(ctx, bot, data)
        await click(ctx, bot, data)
        assert len(attempted) == 1 and not ctx.publish_tasks
        report = await ctx.db.get_report(report_id)
        assert report["status"] == "open" and report["reply_status"] == expected
        if error == "blocked":
            monkeypatch.setattr(bot, "send_message", original_send)
            state = await draft(ctx, bot, report_id, text="改寫後的回覆")
            await click(ctx, bot, f"admin:reply_send:{report_id}:" + state["payload"]["nonce"])
            assert (await ctx.db.get_report(report_id))["reply_status"] == "sent"
        else:
            await click(ctx, bot, f"admin:report_reply:{report_id}")
            assert (await ctx.db.get_state(99))["state"] == "admin_report_detail"
        await close(ctx, bot, report_id)
    report_id = None
    asyncio.run(scenario())


def test_edit_cancel_and_other_admin_closure_invalidate_reply_drafts(ctx, bot):
    async def scenario():
        report_id = (await seed(ctx))[43]
        old = await draft(ctx, bot, report_id)
        await click(ctx, bot, f"admin:reply_edit:{report_id}:" + old["payload"]["nonce"])
        await private_text(bot.message(user_id=99, text="delete"), bot, ctx)
        current = await ctx.db.get_state(99)
        assert current["state"] == "admin_report_reply_confirm"
        assert current["payload"]["nonce"] != old["payload"]["nonce"]
        await click(ctx, bot, f"admin:reply_send:{report_id}:" + old["payload"]["nonce"])
        await ctx.db.close_report(report_id)
        await click(ctx, bot, f"admin:reply_send:{report_id}:" + current["payload"]["nonce"])
        assert not [message for message in bot.messages if message["chat_id"] == 43]
        other = (await ctx.db.list_open_reports(10, 0))[0]["report_id"]
        state = await draft(ctx, bot, other)
        await cancel_command(bot.message(user_id=99, text="/cancel"), bot, ctx)
        await click(ctx, bot, f"admin:reply_send:{other}:" + state["payload"]["nonce"], panel_id=state["payload"]["panel_message_id"])
        assert await ctx.db.get_state(99) is None
        assert (await ctx.db.get_report(other))["reply_status"] == "none"
    asyncio.run(scenario())


@pytest.mark.parametrize("text,valid", [(" \n ", False), ("a" * 4096, False), ("😀" * 2048, False),
                                      ("字" * 3900, True), ("/notice 處理結果", True)])
def test_reply_length_includes_header_and_plain_text_is_not_submitted(ctx, bot, text, valid):
    async def scenario():
        report_id = (await seed(ctx))[43]
        state = await draft(ctx, bot, report_id, text=text)
        assert state["state"] == ("admin_report_reply_confirm" if valid else "admin_wait_report_reply")
        assert await ctx.db.get_pending_for_user(99) is None
        assert not [message for message in bot.messages if message["chat_id"] == 43]
        assert all(len(edit["text"].encode("utf-16-le")) // 2 <= 4096 for edit in bot.edits)
        if valid:
            await click(ctx, bot, f"admin:reply_send:{report_id}:" + state["payload"]["nonce"])
            sent = [message["text"] for message in bot.messages if message["chat_id"] == 43]
            assert len(sent) == 1 and sent[0].endswith(text)
            assert len(sent[0].encode("utf-16-le")) // 2 <= 4096
    asyncio.run(scenario())


def test_report_list_filters_article_and_selects_individual_reports(ctx, bot):
    async def scenario():
        reports = await seed(ctx, reporters=(43, 44, 45, 46))
        await ctx.db.create_article("XG_IJKLMNOP", 50, "other")
        await ctx.db.create_report("XG_IJKLMNOP", 51, "別篇原因", iso_utc(), iso_utc(utc_now() - timedelta(minutes=10)))
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await click(ctx, bot, f"admin:article_reports:{ARTICLE}:0")
        assert "別篇原因" not in bot.edits[-1]["text"]
        buttons = [button for row in bot.edits[-1]["reply_markup"].inline_keyboard for button in row]
        assert any(button.callback_data == f"admin:article_reports:{ARTICLE}:1" for button in buttons)
        assert all(int(button.callback_data.split(":")[-1]) in reports.values() for button in buttons if button.callback_data.startswith("admin:report:"))
        await click(ctx, bot, f"admin:article_reports:{ARTICLE}:1")
        assert "別篇原因" not in bot.edits[-1]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("manual", [False, True])
def test_deleting_or_manually_resolving_article_does_not_close_reports(ctx, bot, manual):
    async def scenario():
        report_id = (await seed(ctx))[43]
        if manual:
            await ctx.db.mark_publish_uncertain(ARTICLE, "unknown_result")
        else:
            await ctx.db.save_article_messages(ARTICLE, ctx.settings.channel_id, [7])
            await ctx.db.mark_published(ARTICLE, iso_utc())
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        action = "resolve" if manual else "force"
        await click(ctx, bot, f"admin:{action}:{ARTICLE}")
        nonce = (await ctx.db.get_state(99))["payload"]["nonce"]
        await click(ctx, bot, f"admin:{action}_yes:{ARTICLE}:{nonce}")
        assert (await ctx.db.get_article(ARTICLE))["status"] == "deleted"
        assert (await ctx.db.get_report(report_id))["status"] == "open"
    asyncio.run(scenario())


def test_interrupted_reply_remains_uncertain_and_cannot_be_replayed(ctx, bot, monkeypatch):
    original_send = bot.send_message

    async def send(**kwargs):
        if kwargs["chat_id"] == 43:
            raise asyncio.CancelledError()
        return await original_send(**kwargs)
    monkeypatch.setattr(bot, "send_message", send)

    async def scenario():
        report_id = (await seed(ctx))[43]
        state = await draft(ctx, bot, report_id)
        data = f"admin:reply_send:{report_id}:" + state["payload"]["nonce"]
        with pytest.raises(asyncio.CancelledError):
            await click(ctx, bot, data)
        assert not ctx.publish_tasks
        assert (await ctx.db.get_report(report_id))["reply_status"] == "uncertain"
        await click(ctx, bot, data)
        assert not [message for message in bot.messages if message["chat_id"] == 43]
    asyncio.run(scenario())


def test_reply_and_close_history_records_each_admin_without_reply_text(ctx, bot, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_admin_user_ids=(100,))
    monkeypatch.setattr("app.database.iso_utc", lambda: "2026-10-19T04:30:00+00:00")

    async def scenario():
        report_id = (await seed(ctx))[43]
        state = await draft(ctx, bot, report_id, text="只給檢舉人的私訊", user_id=100)
        await click(ctx, bot, f"admin:reply_send:{report_id}:" + state["payload"]["nonce"], user_id=100)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await close(ctx, bot, report_id)
        actions = await ctx.db.list_report_actions(report_id, 10, 0)
        assert [(row["action"], row["status"], row["admin_user_id"]) for row in actions] == [
            ("close", "closed", 99), ("reply", "sent", 100),
        ]
        assert all(row["created_at_utc"] and row["completed_at_utc"] for row in actions)
        assert actions[1]["message_id"] == (await ctx.db.get_report(report_id))["reply_message_id"]
        assert "只給檢舉人的私訊" not in str(actions)
        await click(ctx, bot, f"admin:report_history:{report_id}:0")
        text = bot.edits[-1]["text"]
        assert "管理員：99" in text and "管理員：100" in text
        assert "結案" in text and "已送出" in text and "Asia/Taipei" in text
        assert "2026/10/19 12:30:00" in text
        assert "只給檢舉人的私訊" not in text
        await click(ctx, bot, f"admin:report:{report_id}")
        callbacks = [button.callback_data for row in bot.edits[-1]["reply_markup"].inline_keyboard for button in row]
        assert "admin:closed_reports:0" in callbacks
        assert f"admin:report_reply:{report_id}" not in callbacks
        assert f"admin:report_close:{report_id}" not in callbacks
    asyncio.run(scenario())


def test_failed_reply_attempt_is_kept_when_another_admin_retries(ctx, bot, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_admin_user_ids=(100,))
    original_send = bot.send_message

    async def rejected(**kwargs):
        if kwargs["chat_id"] == 43:
            raise TelegramForbiddenError(method=SendMessage(**kwargs), message="blocked by user")
        return await original_send(**kwargs)

    async def scenario():
        report_id = (await seed(ctx))[43]
        state = await draft(ctx, bot, report_id)
        monkeypatch.setattr(bot, "send_message", rejected)
        await click(ctx, bot, f"admin:reply_send:{report_id}:" + state["payload"]["nonce"])
        monkeypatch.setattr(bot, "send_message", original_send)
        state = await draft(ctx, bot, report_id, user_id=100)
        await click(ctx, bot, f"admin:reply_send:{report_id}:" + state["payload"]["nonce"], user_id=100)
        actions = await ctx.db.list_report_actions(report_id, 10, 0)
        assert [(row["admin_user_id"], row["status"]) for row in actions] == [(100, "sent"), (99, "failed")]
    asyncio.run(scenario())


def test_closed_report_query_is_paginated_and_keeps_open_cases_out(ctx, bot):
    async def scenario():
        reports = await seed(ctx, reporters=(43, 44, 45, 46, 47))
        for reporter in (43, 44, 45, 46):
            await ctx.db.close_report(reports[reporter], 99)
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        assert any(button.callback_data == "admin:closed_reports:0" for row in bot.messages[-1]["reply_markup"].inline_keyboard for button in row)
        await click(ctx, bot, "admin:closed_reports:0")
        first = bot.edits[-1]
        assert "已結案檢舉" in first["text"] and "檢舉原因 47" not in first["text"]
        callbacks = [button.callback_data for row in first["reply_markup"].inline_keyboard for button in row]
        assert "admin:closed_reports:1" in callbacks
        assert len([data for data in callbacks if data.startswith("admin:report:")]) == 3
        await click(ctx, bot, "admin:closed_reports:1")
        assert "檢舉原因 43" in bot.edits[-1]["text"] and "檢舉原因 47" not in bot.edits[-1]["text"]
        assert await ctx.db.count_open_reports() == 1
    asyncio.run(scenario())


def test_report_history_is_paginated_and_unknown_legacy_actors_are_not_invented(ctx, bot):
    async def scenario():
        report_id = (await seed(ctx))[43]
        await admin_command(bot.message(user_id=99, text="/admin"), bot, ctx)
        await click(ctx, bot, f"admin:report_history:{report_id}:0")
        assert "尚無處理紀錄" in bot.edits[-1]["text"]
        for _ in range(6):
            assert await ctx.db.claim_report_reply(report_id, 99)
            await ctx.db.finish_report_reply(report_id, "failed")
        await click(ctx, bot, f"admin:report_history:{report_id}:0")
        first = bot.edits[-1]
        assert first["text"].count("管理員：99") == 5
        assert any(button.callback_data == f"admin:report_history:{report_id}:1" for row in first["reply_markup"].inline_keyboard for button in row)
        await click(ctx, bot, f"admin:report_history:{report_id}:1")
        assert bot.edits[-1]["text"].count("管理員：99") == 1
        edits = len(bot.edits)
        panel_id = (await ctx.db.get_state(99))["payload"]["panel_message_id"]
        await click(ctx, bot, f"admin:report_history:{report_id}:0", user_id=42, panel_id=panel_id)
        await click(ctx, bot, "admin:closed_reports:0", user_id=42, panel_id=panel_id)
        assert len(bot.edits) == edits
    asyncio.run(scenario())
