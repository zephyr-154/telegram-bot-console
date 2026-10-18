from __future__ import annotations

from datetime import datetime, timezone

from app.utils import (
    content_hmac,
    iso_utc,
    normalize_article_id,
    normalize_content,
    article_status_text,
    message_link,
)


def test_article_id_accepts_hash_and_normalizes_case() -> None:
    assert normalize_article_id(" #xg_ab2cd3ef ") == "XG_AB2CD3EF"
    assert normalize_article_id("XG_A7K9Q2") is None
    assert normalize_article_id("XG_ABCDEFG123") is None
    assert normalize_article_id("XG_ABCDEFGH2") is None


def test_content_normalization_keeps_case_and_punctuation() -> None:
    assert normalize_content("  A\tB\nC  ") == "A B C"
    assert normalize_content("Hello!") != normalize_content("hello!")
    assert content_hmac(1, "A\nB", "s" * 32) == content_hmac(1, " A  B ", "s" * 32)
    assert content_hmac(1, "Hello", "s" * 32) != content_hmac(2, "Hello", "s" * 32)


def test_iso_utc_always_contains_utc_offset() -> None:
    value = iso_utc(datetime(2026, 8, 22, 12, 34, 56, tzinfo=timezone.utc))
    assert value == "2026-08-22T12:34:56+00:00"


def test_post_link_and_deletion_deadline_are_clear(monkeypatch) -> None:
    monkeypatch.setattr("app.utils.utc_now", lambda: datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc))
    assert message_link(-1001234, 8) == "https://t.me/c/1234/8"
    assert message_link(-42, 8) is None
    article = {"status": "published", "published_at_utc": "2026-10-15T00:01:00+00:00"}
    assert "剩餘約 0 小時 1 分" in article_status_text(article, "Asia/Taipei")
    article["published_at_utc"] = "2026-10-15T00:00:00+00:00"
    assert "已超過" in article_status_text(article, "Asia/Taipei")
