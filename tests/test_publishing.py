from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendPhoto

from app.image_renderer import ImageLayoutError
from app.services.publishing import _wait_for_send_slot, publish_pending
from app.utils import iso_utc, utc_now
from conftest import make_pending


def test_lost_response_is_retained_across_restart_and_blocks_repost(ctx, bot, monkeypatch):
    async def lose_response(**kwargs):
        bot.photo_calls.append(kwargs)
        raise TimeoutError("server accepted; response was lost")
    monkeypatch.setattr(bot, "send_photo", lose_response)

    async def scenario():
        first = await make_pending(ctx)
        success, article_id, error = await publish_pending(ctx, bot, first)
        assert not success and article_id and "核對" in error
        article = await ctx.db.get_article(article_id)
        assert article["send_started_at_utc"] and article["publish_error_code"]
        await ctx.db.close()
        await ctx.db.open()
        assert await ctx.db.discard_incomplete_articles() == 0
        assert await ctx.db.get_article(article_id)
        assert await ctx.db.has_recent_duplicate(42, article["content_hmac"], iso_utc(utc_now() + timedelta(days=2)))
        second = await make_pending(ctx, nonce="retry")
        assert not (await publish_pending(ctx, bot, second))[0]
        assert len(bot.photo_calls) == 1
        assert len(await ctx.db.list_attention_articles(10, 0)) == 1
    asyncio.run(scenario())


def test_same_submission_nonce_is_sent_once(ctx, bot):
    async def scenario():
        pending = await make_pending(ctx)
        first, second = await asyncio.gather(publish_pending(ctx, bot, pending), publish_pending(ctx, bot, pending))
        assert first[0] and second[0] and first[1] == second[1]
        assert len(bot.photo_calls) == 1
    asyncio.run(scenario())


def test_retry_after_waits_and_reuses_article_id(ctx, bot, monkeypatch):
    original = bot.send_photo
    attempts, sleeps = [], []

    async def send(**kwargs):
        attempts.append(kwargs["caption"])
        if len(attempts) == 1:
            raise TelegramRetryAfter(method=SendPhoto(chat_id=-1001234, photo="unused"), message="flood", retry_after=2)
        return await original(**kwargs)

    async def sleep(delay):
        sleeps.append(delay)

    async def slot(*args):
        pass

    monkeypatch.setattr(bot, "send_photo", send)
    monkeypatch.setattr("app.services.publishing.asyncio.sleep", sleep)
    monkeypatch.setattr("app.services.publishing._wait_for_send_slot", slot)

    async def scenario():
        result = await publish_pending(ctx, bot, await make_pending(ctx))
        assert result[0] and attempts[0] == attempts[1]
        assert sum(sleeps) >= 2
        article = await ctx.db.get_article(result[1])
        assert article["status"] == "published" and article["publish_error_code"] is None
    asyncio.run(scenario())


def test_target_limit_counts_album_photos(ctx, monkeypatch):
    clock = [100.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("app.services.publishing.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.services.publishing.asyncio.sleep", sleep)

    async def scenario():
        await _wait_for_send_slot(ctx, 10)
        await _wait_for_send_slot(ctx, 10)
        await _wait_for_send_slot(ctx, 1)
        assert sum(sleeps) >= 60
        assert len(ctx.send_times) <= 20
    asyncio.run(scenario())


def test_rendering_runs_off_event_loop_and_queue_honors_pause(ctx, bot, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def render(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return [b"png"]

    monkeypatch.setattr("app.services.publishing.render_post_images", render)

    async def scenario():
        one = await make_pending(ctx)
        two = await make_pending(ctx, user_id=43, nonce="second")
        first = asyncio.create_task(publish_pending(ctx, bot, one))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set() and not first.done()
            second = asyncio.create_task(publish_pending(ctx, bot, two))
            await ctx.db.set_submissions_paused(True)
        finally:
            release.set()
        results = await asyncio.gather(first, second)
        assert all(not result[0] and "暫停" in result[2] for result in results)
        assert not bot.photo_calls
    asyncio.run(scenario())


def test_expired_draft_is_not_rendered_or_published(ctx, bot):
    async def scenario():
        pending = await make_pending(ctx, created=iso_utc(utc_now() - timedelta(hours=25)))
        result = await publish_pending(ctx, bot, pending)
        assert not result[0] and "24 小時" in result[2]
        assert not bot.photo_calls
    asyncio.run(scenario())


def test_layout_error_tells_author_to_shorten_text(ctx, bot, monkeypatch):
    def render(*args, **kwargs):
        raise ImageLayoutError("too many lines")
    monkeypatch.setattr("app.services.publishing.render_post_images", render)

    async def scenario():
        result = await publish_pending(ctx, bot, await make_pending(ctx))
        assert not result[0] and "減少空行" in result[2]
        assert not bot.photo_calls
    asyncio.run(scenario())


def test_known_rejection_does_not_leave_uncertain_article(ctx, bot, monkeypatch):
    async def reject(**kwargs):
        raise TelegramBadRequest(method=SendPhoto(chat_id=-1001234, photo="unused"), message="not enough rights")
    monkeypatch.setattr(bot, "send_photo", reject)

    async def scenario():
        result = await publish_pending(ctx, bot, await make_pending(ctx))
        assert not result[0] and "拒絕" in result[2]
        assert not await ctx.db.list_attention_articles(10, 0)
        assert not await ctx.db.list_user_articles(42, 10, 0)
    asyncio.run(scenario())


def test_shutdown_during_send_preserves_unknown_result(ctx, bot, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        async def send(**kwargs):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(bot, "send_photo", send)
        task = asyncio.create_task(publish_pending(ctx, bot, await make_pending(ctx)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        article = (await ctx.db.list_attention_articles(10, 0))[0]
        assert article["send_started_at_utc"] and article["publish_error_code"] == "interrupted_publish"
        assert not ctx.publish_tasks
    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_database_failure_after_send_rolls_back_or_retains_message_ids(ctx, bot, monkeypatch, cleanup_fails):
    async def fail_save(*args):
        raise sqlite3.OperationalError("simulated database write failure")
    monkeypatch.setattr(ctx.db, "mark_published", fail_save)
    if cleanup_fails:
        async def fail_delete(**kwargs):
            raise RuntimeError("simulated Telegram cleanup failure")
        monkeypatch.setattr(bot, "delete_message", fail_delete)

    async def scenario():
        success, article_id, error = await publish_pending(ctx, bot, await make_pending(ctx))
        assert not success and len(bot.photo_calls) == 1
        if cleanup_fails:
            assert article_id and "核對" in error
            assert await ctx.db.get_article_messages(article_id)
            assert (await ctx.db.get_article(article_id))["publish_error_code"]
        else:
            assert article_id is None and "撤回" in error
            assert bot.deleted and not await ctx.db.list_user_articles(42, 10, 0)
    asyncio.run(scenario())
