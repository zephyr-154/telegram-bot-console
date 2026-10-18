from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramConflictError, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.methods import GetChatAdministrators, GetMe, GetUpdates
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message, Update, User

from app import chat_discovery
from app.backup import database_lock
from app.chat_discovery import ChatRegistry, ChatTrackingMiddleware, administrator_candidates, fetch_administrators, watch_chats
from app.config import ConfigurationError, read_dotenv
from app.manager import BotManager
from app.storage import write_json


TOKEN = "9988776655:offline_test_token"
BOT_USER = User(id=9988776655, is_bot=True, first_name="Test bot")
ACTOR = User(id=42, is_bot=False, first_name="Invite Admin")
AUTHOR = User(id=43, is_bot=False, first_name="Not saved")
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def joined(number=1, *, chat_id=-1001, title="測試群組", status="member", kind="supergroup"):
    member = ChatMemberLeft(user=BOT_USER) if status == "left" else ChatMemberMember(user=BOT_USER)
    return Update(update_id=number, my_chat_member=ChatMemberUpdated(
        chat=Chat(id=chat_id, type=kind, title=title), from_user=ACTOR, date=NOW,
        old_chat_member=ChatMemberLeft(user=BOT_USER), new_chat_member=member,
    ))


def message(number=2, *, chat_id=-1001, title="測試群組", text="不應儲存的投稿", kind="supergroup"):
    return Update(update_id=number, **{"channel_post" if kind == "channel" else "message": Message(
        message_id=number, date=NOW, chat=Chat(id=chat_id, type="private" if chat_id > 0 else kind, title=title),
        from_user=AUTHOR, text=text,
    )})


@pytest.fixture
def manager(tmp_path):
    result = BotManager(tmp_path / "managed")
    result.create("school")
    return result


class FakeBot:
    def __init__(self):
        self.batches, self.calls, self.closed = [], [], False
        self.error, self.webhook = None, ""
        self.chats = {-1001: Chat(id=-1001, type="supergroup", title="測試群組")}
        self.members = {}
        self.administrators = {}
        self.failed_admin_chats = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True
        await self.session.close()

    async def get_me(self):
        if self.error:
            raise self.error
        return SimpleNamespace(id=BOT_USER.id, username="OfflineTestBot")

    async def get_webhook_info(self):
        self.calls.append("webhook")
        return SimpleNamespace(url=self.webhook)

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.batches.pop(0) if self.batches else []

    async def get_chat(self, chat_id):
        return self.chats[chat_id]

    async def get_chat_member(self, chat_id, user_id):
        assert user_id == BOT_USER.id
        return SimpleNamespace(status=self.members.get(chat_id, "member"), is_member=True)

    async def get_chat_administrators(self, chat_id):
        self.calls.append(("admins", chat_id))
        if chat_id in self.failed_admin_chats:
            raise TelegramForbiddenError(method=GetChatAdministrators(chat_id=chat_id), message=TOKEN)
        return [SimpleNamespace(user=person) for person in self.administrators.get(chat_id, [])]


@pytest.fixture
def fake_bot(monkeypatch):
    bot = FakeBot()
    def create(*, token, session):
        assert token == TOKEN
        bot.session = session
        return bot
    monkeypatch.setattr(chat_discovery, "Bot", create)
    return bot


def test_registry_only_records_chat_metadata_and_separates_bot_id(manager):
    folder = manager.folder("school")
    registry = ChatRegistry(folder, TOKEN)
    registry.observe(message(chat_id=42))
    assert not registry.path.exists()
    registry.observe(joined(title="新工\x1b\n爆言板", kind="channel"))
    assert registry.chats()[0]["title"] == "新工爆言板"
    content = registry.path.read_text(encoding="utf-8")
    assert TOKEN not in content and "不應儲存" not in content and "Not saved" not in content
    registry.observe(message(title="新工爆言板", kind="channel"))
    assert registry.path.read_text(encoding="utf-8") == content
    assert ChatRegistry(folder, "9988776655:rotated_token").chats() == registry.chats()
    assert ChatRegistry(folder, "8877665544:other_bot").chats() == []


def test_removed_chat_stays_removed_when_old_updates_are_replayed_then_can_rejoin(manager):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    registry.observe(joined(1))
    registry.observe(joined(3, status="left"))
    registry.observe(message(2))
    assert registry.chats() == []
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    registry.observe(joined(1))
    assert registry.chats() == []
    registry.observe(joined(4, title="重新加入"))
    assert registry.chats()[0]["title"] == "重新加入"
    registry.observe(joined(6, title="重新加入"))
    registry.observe(joined(5, title="重新加入", status="left"))
    assert registry.chats()[0]["title"] == "重新加入"


def test_registry_ignores_corrupt_records_and_rejects_symlink(manager, tmp_path):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    write_json(registry.path, {"bot_id": BOT_USER.id, "chats": [None, {"id": -1, "type": []}, {"id": True}]})
    assert registry.chats() == []
    registry.path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    try:
        registry.path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links require privileges on this Windows host")
    with pytest.raises(ConfigurationError):
        registry.observe(joined())
    assert target.read_text(encoding="utf-8") == "{}"


def test_token_only_listener_receives_new_join_without_consuming_pending_submissions(manager, fake_bot):
    fake_bot.batches = [[message(chat_id=42)], [message(chat_id=42), joined(3)]]
    async def scenario():
        listener = watch_chats(manager, "school", TOKEN)
        chats, notice = await anext(listener)
        assert chats == [] and "等待邀請 @OfflineTestBot" in notice
        with pytest.raises(RuntimeError), database_lock(manager.folder("school") / "worker"):
            pass
        assert (await anext(listener))[0] == []
        chats, _ = await anext(listener)
        assert chats[0]["id"] == -1001
        calls = [call for call in fake_bot.calls if isinstance(call, dict)]
        assert len(calls) == 2
        assert all("offset" not in call for call in calls)
        assert all({"my_chat_member", "message", "callback_query", "channel_post"} <= set(call["allowed_updates"]) for call in calls)
        await listener.aclose()
        assert fake_bot.closed
        with database_lock(manager.folder("school") / "worker"):
            pass
    asyncio.run(scenario())


def test_running_worker_uses_its_registry_without_second_update_receiver(manager, fake_bot, monkeypatch):
    folder = manager.folder("school")
    write_json(folder / "runtime.json", {"bot_id": str(BOT_USER.id), "chat_tracking": True})
    monkeypatch.setattr(manager, "process", lambda name: object())
    registry = ChatRegistry(folder, TOKEN)
    registry.observe(joined())
    async def scenario():
        listener = watch_chats(manager, "school", TOKEN)
        assert (await anext(listener))[0][0]["id"] == -1001
        registry.observe(joined(2, chat_id=-1002, title="新頻道", kind="channel"))
        assert len((await anext(listener))[0]) == 2
        await listener.aclose()
        assert fake_bot.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("state,match", [
    ({"bot_id": "8877665544", "chat_tracking": True}, "另一個 Token"),
    ({"bot_id": str(BOT_USER.id)}, "重啟"),
])
def test_running_worker_with_other_token_or_old_version_requires_action(manager, fake_bot, monkeypatch, state, match):
    write_json(manager.folder("school") / "runtime.json", state)
    monkeypatch.setattr(manager, "process", lambda name: object())
    async def scenario():
        with pytest.raises(ConfigurationError, match=match):
            await anext(watch_chats(manager, "school", TOKEN))
    asyncio.run(scenario())
    assert not fake_bot.calls


def test_same_bot_in_another_profile_is_rejected_before_connecting(manager, fake_bot):
    other = manager.create("other")
    (other / ".env").write_text(f"BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    async def scenario():
        with pytest.raises(ConfigurationError, match="other"):
            await anext(watch_chats(manager, "school", TOKEN))
    asyncio.run(scenario())
    assert not fake_bot.calls


def test_webhook_is_preserved_and_listener_lock_is_released(manager, fake_bot):
    fake_bot.webhook = "https://example.invalid/webhook"
    async def scenario():
        with pytest.raises(ConfigurationError, match="Webhook"):
            await anext(watch_chats(manager, "school", TOKEN))
        assert fake_bot.closed and fake_bot.calls == ["webhook"]
        with database_lock(manager.folder("school") / "worker"):
            pass
    asyncio.run(scenario())


@pytest.mark.parametrize("error,match", [
    (TelegramUnauthorizedError(method=GetMe(), message=TOKEN), "Token 無效"),
    (TelegramConflictError(method=GetUpdates(), message=TOKEN), "其他程式"),
])
def test_telegram_errors_do_not_expose_token_and_release_resources(manager, fake_bot, error, match):
    fake_bot.error = error
    async def scenario():
        with pytest.raises(ConfigurationError, match=match) as raised:
            await anext(watch_chats(manager, "school", TOKEN))
        assert TOKEN not in str(raised.value) and fake_bot.closed
        with database_lock(manager.folder("school") / "worker"):
            pass
    asyncio.run(scenario())


def test_full_queue_shows_limit_without_dropping_or_confirming_updates(manager, fake_bot):
    fake_bot.batches = [[message(number, chat_id=42) for number in range(100)]]
    async def scenario():
        listener = watch_chats(manager, "school", TOKEN)
        await anext(listener)
        _, notice = await anext(listener)
        assert "100 筆" in notice and "手動" in notice
        assert "offset" not in fake_bot.calls[-1]
        await listener.aclose()
    asyncio.run(scenario())


def test_refresh_checks_current_membership_and_updates_name(manager, fake_bot):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    registry.observe(joined(title="舊名稱"))
    fake_bot.chats[-1001] = Chat(id=-1001, title="新名稱", type="supergroup")
    fake_bot.members[-1001] = "left"
    fake_bot.batches = [[joined(title="舊名稱")]]
    async def scenario():
        listener = watch_chats(manager, "school", TOKEN)
        await anext(listener)
        assert (await anext(listener))[0] == []
        assert registry._entries()[-1001]["title"] == "新名稱"
        await listener.aclose()
    asyncio.run(scenario())


def test_cancelling_listener_closes_session_and_does_not_block_start(manager, fake_bot):
    async def scenario():
        listener = watch_chats(manager, "school", TOKEN)
        await anext(listener)
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        fake_bot.get_updates = blocked
        task = asyncio.create_task(anext(listener))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_bot.closed
        with database_lock(manager.folder("school") / "worker"):
            pass
    asyncio.run(scenario())


def test_tracking_failure_does_not_interrupt_submission_handler(manager, monkeypatch):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    def broken(update):
        raise OSError("cannot write")
    monkeypatch.setattr(registry, "observe", broken)
    update = message(chat_id=42)
    async def handler(event, data):
        assert event is update and data == {"ctx": "existing"}
        return "submission processed"
    assert asyncio.run(ChatTrackingMiddleware(registry)(handler, update, {"ctx": "existing"})) == "submission processed"


def test_tracking_retries_a_short_registry_lock_conflict(manager, monkeypatch):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    observe, attempts = registry.observe, []
    def briefly_busy(update):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("locked")
        observe(update)
    monkeypatch.setattr(registry, "observe", briefly_busy)
    async def handler(event, data):
        return "processed once"
    assert asyncio.run(ChatTrackingMiddleware(registry)(handler, joined(), {})) == "processed once"
    assert len(attempts) == 2 and registry.chats()[0]["id"] == -1001


def test_only_real_join_records_inviter_and_promotion_does_not_replace_it(manager):
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    registry.observe(joined())
    assert registry.chats()[0]["inviter"]["id"] == ACTOR.id
    values = {key: False for key, field in ChatMemberAdministrator.model_fields.items() if field.is_required()}
    values.update(user=BOT_USER, status="administrator")
    promotion = joined(2).model_copy(update={"my_chat_member": joined(2).my_chat_member.model_copy(update={
        "old_chat_member": ChatMemberMember(user=BOT_USER),
        "new_chat_member": ChatMemberAdministrator(**values),
        "from_user": User(id=99, is_bot=False, first_name="Promoter"),
    })})
    registry.observe(promotion)
    assert registry.chats()[0]["inviter"]["id"] == ACTOR.id
    registry.observe(joined(3, status="left"))
    anonymous_rejoin = joined(4).model_copy(update={"my_chat_member": joined(4).my_chat_member.model_copy(update={"from_user": BOT_USER})})
    registry.observe(anonymous_rejoin)
    assert registry.chats()[0]["inviter"] is None


def test_live_admin_fetch_deduplicates_people_filters_bots_and_never_grants_access(manager, fake_bot):
    fake_bot.chats[-1002] = Chat(id=-1002, title="Other", type="channel")
    another = User(id=84, is_bot=False, first_name="Another admin", username="another")
    renamed = ACTOR.model_copy(update={"first_name": "Current Admin", "username": "current"})
    fake_bot.administrators = {-1001: [renamed, BOT_USER], -1002: [renamed, another]}
    registry = ChatRegistry(manager.folder("school"), TOKEN)
    registry.observe(joined())
    async def scenario():
        found, failed = await fetch_administrators(manager, "school", TOKEN, "-1001,-1002")
        assert failed == [] and fake_bot.closed
        people = administrator_candidates(found, (-1001, -1002))
        assert {person["id"] for person in people} == {42, 84}
        assert set(next(person for person in people if person["id"] == 42)["sources"]) == {"邀請人", "群組管理員"}
        assert next(person for person in people if person["id"] == 42)["name"] == "Current Admin"
        assert [person["id"] for person in administrator_candidates(found, (-1001,))] == [42]
        assert fake_bot.calls == [("admins", -1001), ("admins", -1002)]
        assert read_dotenv(manager.folder("school") / ".env")["ADMIN_USER_ID"] == ""
    asyncio.run(scenario())


def test_admin_fetch_keeps_successful_groups_when_another_is_inaccessible(manager, fake_bot):
    fake_bot.chats[-1002] = Chat(id=-1002, title="Other", type="channel")
    fake_bot.administrators[-1001] = [ACTOR]
    fake_bot.failed_admin_chats.add(-1002)
    async def scenario():
        found, failed = await fetch_administrators(manager, "school", TOKEN, "-1001,-1002")
        assert failed == [-1002]
        assert [person["id"] for person in administrator_candidates(found)] == [42]
        assert fake_bot.closed
    asyncio.run(scenario())
