from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendPhoto

from app.config import ConfigurationError, parse_ids, settings_from_values
from app.handlers.admin import admin_callback
from app.handlers.private import admin_command, private_text
from app.main import _validate_destination
from app.services.admin import notify_admin
from app.services.deletion import delete_article_messages
from app.services.publishing import publish_pending
from app.settings_editor import DEFAULTS
from app.utils import utc_now
from conftest import FakeCallback, make_pending


@pytest.fixture
def multi(ctx, monkeypatch):
    ctx.settings = replace(ctx.settings, additional_channel_ids=(-1002000, -1001234), additional_admin_user_ids=(100, 99))
    async def slot(*args):
        pass
    monkeypatch.setattr("app.services.publishing._wait_for_send_slot", slot)
    return ctx


def test_csv_ids_preserve_single_value_compatibility_and_deduplicate(tmp_path):
    values = DEFAULTS | {"BOT_TOKEN": "9988776655:offline", "CHANNEL_ID": "-1001， -1002,-1001",
                         "ADMIN_USER_ID": "42,84,42", "CONTENT_HASH_SECRET": "s" * 32}
    settings = settings_from_values(values, tmp_path)
    assert settings.channel_id == -1001 and settings.channel_ids == (-1001, -1002)
    assert settings.admin_user_id == 42 and settings.admin_user_ids == (42, 84)
    for key, value in (("CHANNEL_ID", "-1,2"), ("ADMIN_USER_ID", "42,-84"),
                       ("CHANNEL_ID", "-1,"), ("ADMIN_USER_ID", "42,,84")):
        with pytest.raises(ConfigurationError):
            parse_ids(value, key)


def test_each_admin_has_an_independent_panel_and_unlisted_users_stay_unauthorized(multi, bot):
    async def scenario():
        for user_id in (99, 100):
            await admin_command(bot.message(user_id=user_id, text="/hhvszephyr"), bot, multi)
        states = {user_id: await multi.db.get_state(user_id) for user_id in (99, 100)}
        panels = {user_id: states[user_id]["payload"]["panel_message_id"] for user_id in states}
        assert panels[99] != panels[100]
        await admin_callback(FakeCallback(bot, "admin:pause", user_id=100, message_id=panels[100]), bot, multi)
        await admin_callback(FakeCallback(bot, "admin:set_pause:1", user_id=100, message_id=panels[100]), bot, multi)
        assert await multi.db.submissions_paused()
        before = len(bot.messages), len(bot.edits)
        await admin_command(bot.message(user_id=101, text="/hhvszephyr"), bot, multi)
        await admin_callback(FakeCallback(bot, "admin:stats", user_id=101, message_id=panels[99]), bot, multi)
        cross_panel = FakeCallback(bot, "admin:stats", user_id=100, message_id=panels[99])
        cross_panel.message.chat.id = 99
        await admin_callback(cross_panel, bot, multi)
        assert (len(bot.messages), len(bot.edits)) == before
        assert (await multi.db.get_state(99))["payload"]["panel_message_id"] == panels[99]
    asyncio.run(scenario())


def test_notifications_reach_other_admins_when_one_is_unreachable(multi, bot, monkeypatch):
    recipients = []
    async def send(**kwargs):
        recipients.append(kwargs["chat_id"])
        if kwargs["chat_id"] == 99:
            raise RuntimeError("user has not started bot")
    monkeypatch.setattr(bot, "send_message", send)
    asyncio.run(notify_admin(multi, bot, "待處理事項"))
    assert recipients == [99, 100]


def test_startup_validates_every_target_and_identifies_the_invalid_one(multi):
    checked = []
    class Bot:
        async def get_chat(self, chat_id):
            checked.append(chat_id)
            return SimpleNamespace(type="supergroup", permissions=None)
        async def get_me(self):
            return SimpleNamespace(id=123)
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left" if chat_id == -1002000 else "member")
    with pytest.raises(ConfigurationError, match="-1002000"):
        asyncio.run(_validate_destination(multi, Bot()))
    assert checked == [-1001234, -1002000]


@pytest.mark.parametrize("pages", [1, 2])
def test_render_once_publish_all_targets_and_delete_all_recorded_copies(multi, bot, monkeypatch, pages):
    renders = []
    def render(*args, **kwargs):
        renders.append(True)
        return [b"png"] * pages
    monkeypatch.setattr("app.services.publishing.render_post_images", render)
    async def photo(**kwargs):
        bot.photo_calls.append(kwargs)
        return SimpleNamespace(message_id=7, date=utc_now())
    async def album(**kwargs):
        bot.media_calls.append(kwargs)
        return [SimpleNamespace(message_id=7 + index, date=utc_now()) for index in range(pages)]
    monkeypatch.setattr(bot, "send_photo", photo)
    monkeypatch.setattr(bot, "send_media_group", album)
    async def scenario():
        success, article_id, error = await publish_pending(multi, bot, await make_pending(multi))
        assert success and error is None and renders == [True]
        calls = bot.photo_calls if pages == 1 else bot.media_calls
        assert [call["chat_id"] for call in calls] == [-1001234, -1002000]
        rows = await multi.db.get_article_messages(article_id)
        expected = {(target, 7 + index) for target in multi.settings.channel_ids for index in range(pages)}
        assert {(row["channel_id"], row["message_id"]) for row in rows} == expected
        assert [row["sequence_no"] for row in rows] == list(range(1, pages * 2 + 1))
        assert len(await multi.db.list_user_articles(42, 10, 0)) == 1
        article = await multi.db.get_article(article_id)
        assert json.loads(article["target_chat_ids"]) == [-1001234, -1002000]
        multi.settings = replace(multi.settings, channel_id=-1009999, additional_channel_ids=())
        assert (await delete_article_messages(multi, bot, article_id)).success
        assert {(entry["chat_id"], entry["message_id"]) for entry in bot.deleted} == expected
    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_second_target_rejection_rolls_back_or_retains_known_copies(multi, bot, monkeypatch, cleanup_fails):
    original = bot.send_photo
    async def photo(**kwargs):
        if kwargs["chat_id"] == -1002000:
            raise TelegramBadRequest(method=SendPhoto(chat_id=-1002000, photo="unused"), message="not enough rights")
        return await original(**kwargs)
    monkeypatch.setattr(bot, "send_photo", photo)
    if cleanup_fails:
        async def fail_delete(**kwargs):
            raise RuntimeError("cannot delete")
        monkeypatch.setattr(bot, "delete_message", fail_delete)
    async def scenario():
        success, article_id, error = await publish_pending(multi, bot, await make_pending(multi))
        assert not success and len(bot.photo_calls) == 1
        if cleanup_fails:
            assert article_id and "核對" in error
            assert (await multi.db.get_article(article_id))["publish_error_code"] == "partial_publish"
            assert len(await multi.db.get_article_messages(article_id)) == 1
            result = await delete_article_messages(multi, bot, article_id)
            assert not result.success and result.error_code == "publication_uncertain"
            assert {call["chat_id"] for call in bot.messages} == {99, 100}
        else:
            assert article_id is None and "拒絕" in error
            assert bot.deleted[0]["chat_id"] == -1001234
            assert not await multi.db.list_user_articles(42, 10, 0)
    asyncio.run(scenario())


def test_unknown_second_response_never_permits_repost_or_marks_untracked_copy_deleted(multi, bot, monkeypatch):
    original, attempts = bot.send_photo, []
    async def photo(**kwargs):
        attempts.append(kwargs["chat_id"])
        if kwargs["chat_id"] == -1002000:
            raise TimeoutError("accepted but response lost")
        return await original(**kwargs)
    monkeypatch.setattr(bot, "send_photo", photo)
    async def scenario():
        success, article_id, error = await publish_pending(multi, bot, await make_pending(multi))
        assert not success and article_id and "核對" in error
        assert attempts == [-1001234, -1002000]
        await multi.db.close()
        await multi.db.open()
        assert await multi.db.discard_incomplete_articles() == 0
        article = await multi.db.get_article(article_id)
        assert json.loads(article["target_chat_ids"]) == [-1001234, -1002000]
        assert (await delete_article_messages(multi, bot, article_id)).error_code == "publication_uncertain"
        assert not (await publish_pending(multi, bot, await make_pending(multi, nonce="retry")))[0]
        assert attempts == [-1001234, -1002000]
    asyncio.run(scenario())


def test_retry_after_only_retries_the_affected_target(multi, bot, monkeypatch):
    attempts, waits = [], []
    original = bot.send_photo
    async def photo(**kwargs):
        target = kwargs["chat_id"]
        attempts.append(target)
        if target == -1002000 and attempts.count(target) == 1:
            raise TelegramRetryAfter(method=SendPhoto(chat_id=target, photo="unused"), message="flood", retry_after=2)
        return await original(**kwargs)
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(bot, "send_photo", photo)
    monkeypatch.setattr("app.services.publishing.asyncio.sleep", sleep)
    async def scenario():
        assert (await publish_pending(multi, bot, await make_pending(multi)))[0]
        assert attempts == [-1001234, -1002000, -1002000] and sum(waits) >= 2
    asyncio.run(scenario())


def test_shutdown_between_targets_preserves_first_copy_and_original_targets(multi, bot, monkeypatch):
    async def scenario():
        between = asyncio.Event()
        calls = []
        async def slot(*args):
            calls.append(True)
            if len(calls) == 2:
                between.set()
                await asyncio.Event().wait()
        monkeypatch.setattr("app.services.publishing._wait_for_send_slot", slot)
        task = asyncio.create_task(publish_pending(multi, bot, await make_pending(multi)))
        await asyncio.wait_for(between.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        article = (await multi.db.list_attention_articles(10, 0))[0]
        assert article["publish_error_code"] == "interrupted_publish"
        assert len(await multi.db.get_article_messages(article["article_id"])) == 1
        assert json.loads(article["target_chat_ids"]) == [-1001234, -1002000]
    asyncio.run(scenario())


def test_pause_between_targets_retracts_first_copy(multi, bot, monkeypatch):
    calls = []
    async def slot(*args):
        calls.append(True)
        if len(calls) == 2:
            await multi.db.set_submissions_paused(True)
    monkeypatch.setattr("app.services.publishing._wait_for_send_slot", slot)
    async def scenario():
        result = await publish_pending(multi, bot, await make_pending(multi))
        assert not result[0] and result[1] is None and "暫停" in result[2]
        assert len(bot.photo_calls) == 1 and len(bot.deleted) == 1
        assert not await multi.db.list_user_articles(42, 10, 0)
    asyncio.run(scenario())


def test_preview_discloses_target_count_and_changed_targets_require_new_confirmation(multi, bot):
    async def scenario():
        await private_text(bot.message(text="一篇新投稿"), bot, multi)
        assert "2 個群組／頻道" in bot.messages[-1]["text"]
        pending = await multi.db.get_pending_for_user(42)
        assert json.loads(pending["target_chat_ids"]) == [-1001234, -1002000]
        multi.settings = replace(multi.settings, additional_channel_ids=(-1003000,))
        result = await publish_pending(multi, bot, pending)
        assert not result[0] and "目標已變更" in result[2] and not bot.photo_calls
    asyncio.run(scenario())
