from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


ARTICLE_ID_RE = re.compile(r"^XG_[A-Z2-9]{8}$")
ARTICLE_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PENDING_TTL = timedelta(hours=24)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def taipei_now(timezone_name: str = "Asia/Taipei") -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def normalize_content(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def content_hmac(user_id: int, text: str, secret: str) -> str:
    payload = f"{user_id}\0{normalize_content(text)}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def generate_article_id() -> str:
    suffix = "".join(secrets.choice(ARTICLE_ID_ALPHABET) for _ in range(8))
    return f"XG_{suffix}"


def normalize_article_id(value: str) -> str | None:
    candidate = value.strip().upper()
    if candidate.startswith("#"):
        candidate = candidate[1:]
    return candidate if ARTICLE_ID_RE.fullmatch(candidate) else None


def clean_submission_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("投稿內容不可為空。")
    if len(text) > 4000:
        raise ValueError("投稿內容不可超過 4000 字，請縮短後再試。")
    return text


def pending_expired(pending: dict) -> bool:
    created_at = parse_datetime(pending.get("created_at_utc"))
    return created_at is None or utc_now() - created_at >= PENDING_TTL


def message_link(channel_id: int, message_id: int) -> str | None:
    chat = str(channel_id)
    if chat.startswith("-100") and len(chat) > 4:
        return f"https://t.me/c/{chat[4:]}/{message_id}"
    return None


def article_status_text(article: dict, timezone_name: str) -> str:
    if article["status"] == "deleted":
        return "已刪除／已人工結案"
    if article["status"] == "publishing":
        return "發布結果等待管理員核對" if article.get("publish_error_code") else "排隊處理中"
    published_at = parse_datetime(article.get("published_at_utc"))
    if not published_at:
        return "發布時間待管理員核對"
    deadline = published_at + timedelta(hours=48)
    seconds = (deadline - utc_now()).total_seconds()
    if seconds <= 0:
        return "已超過自助刪文期限，請聯絡管理員人工處理"
    minutes = max(1, int(seconds / 60))
    local_deadline = deadline.astimezone(ZoneInfo(timezone_name)).strftime("%m/%d %H:%M")
    return f"可自助刪文，剩餘約 {minutes // 60} 小時 {minutes % 60} 分（{local_deadline} 截止）"
