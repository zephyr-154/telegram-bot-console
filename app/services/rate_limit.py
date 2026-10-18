from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.database import Database
from app.utils import iso_utc, taipei_now, utc_now


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    message: str | None = None


async def check_publish_limits(
    db: Database,
    user_id: int,
    timezone_name: str,
) -> RateDecision:
    now = utc_now()
    # SQLite stores timestamps at second precision. Use the next second as
    # the exclusive upper bound so a post created in the current second is
    # counted instead of being lost at the boundary.
    query_end = now + timedelta(seconds=1)
    recent_start = now - timedelta(minutes=10)
    recent_count = await db.count_user_between(
        user_id,
        iso_utc(recent_start),
        iso_utc(query_end),
    )
    if recent_count >= 3:
        oldest = await db.oldest_user_publication_between(
            user_id,
            iso_utc(recent_start),
            iso_utc(query_end),
        )
        wait_message = "請稍後再試。"
        if oldest:
            oldest_at = datetime.fromisoformat(oldest)
            retry_at = oldest_at + timedelta(minutes=10)
            remaining_seconds = max(0, int((retry_at - now).total_seconds()))
            remaining_minutes = max(1, (remaining_seconds + 59) // 60)
            wait_message = f"約 {remaining_minutes} 分鐘後可以再試。"
        return RateDecision(
            False,
            f"發布太頻繁，10 分鐘內最多發布 3 篇，{wait_message}",
        )

    local_now = taipei_now(timezone_name)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = local_start.astimezone(now.tzinfo)
    today_count = await db.count_user_between(
        user_id,
        iso_utc(utc_start),
        iso_utc(query_end),
    )
    if today_count >= 20:
        return RateDecision(False, "今日發布額度已用完，請明天再試。")
    return RateDecision(True)
