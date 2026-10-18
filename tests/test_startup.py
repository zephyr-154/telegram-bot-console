from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import ConfigurationError
from app.main import _validate_destination


@pytest.mark.parametrize("chat_type,status,can_post,can_photo,allowed", [
    ("private", "member", False, None, False),
    ("channel", "administrator", False, None, False),
    ("supergroup", "restricted", False, False, False),
    ("group", "member", False, False, False),
    ("supergroup", "administrator", True, False, True),
    ("channel", "administrator", True, None, True),
])
def test_startup_checks_target_type_and_send_permissions(ctx, chat_type, status, can_post, can_photo, allowed):
    class Bot:
        async def get_chat(self, chat_id):
            return SimpleNamespace(type=chat_type, permissions=SimpleNamespace(can_send_photos=can_photo))
        async def get_me(self):
            return SimpleNamespace(id=123)
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status=status, can_post_messages=can_post, can_send_photos=can_photo)
    if allowed:
        asyncio.run(_validate_destination(ctx, Bot()))
    else:
        with pytest.raises(ConfigurationError):
            asyncio.run(_validate_destination(ctx, Bot()))
