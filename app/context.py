from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.config import Settings
from app.database import Database


@dataclass(slots=True)
class AppContext:
    settings: Settings
    db: Database
    user_locks: defaultdict[int, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))
    article_locks: defaultdict[str, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )
    publish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_times: deque[float] = field(default_factory=deque)
    publish_tasks: set[asyncio.Task] = field(default_factory=set)
