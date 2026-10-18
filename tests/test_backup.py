from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.backup import backup_database, database_lock, restore_database
from app.utils import iso_utc


def test_online_backup_includes_wal_and_keeps_seven_snapshots(ctx):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "digest")
        await ctx.db.mark_published("XG_ABCDEFGH", iso_utc())
        folder = ctx.db.path.parent / "backups"
        first = await asyncio.to_thread(backup_database, ctx.db.path, folder)
        with closing(sqlite3.connect(first)) as saved:
            assert saved.execute("SELECT author_user_id FROM articles").fetchone()[0] == 42
            assert saved.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        for _ in range(7):
            await asyncio.to_thread(backup_database, ctx.db.path, folder)
        assert len(list(folder.glob("bot-backup-*.sqlite3"))) == 7
        assert not first.exists()
        assert await ctx.db.get_article("XG_ABCDEFGH")
    asyncio.run(scenario())


def test_restore_preserves_current_database_as_safety_backup(ctx):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "old")
        await ctx.db.replace_pending(
            user_id=42, source_chat_id=42, source_message_id=1, raw_text="備份中的舊稿",
            callback_nonce="old-draft", created_at_utc=iso_utc(),
        )
        await ctx.db.set_state(42, "delete_wait_id", {}, iso_utc())
        await ctx.db.set_submissions_paused(False)
        backup = backup_database(ctx.db.path, ctx.db.path.parent / "backups")
        with closing(sqlite3.connect(backup)) as snapshot:
            assert snapshot.execute("SELECT raw_text FROM pending_submissions").fetchone()[0] == "備份中的舊稿"
            assert snapshot.execute("SELECT COUNT(*) FROM user_states").fetchone()[0] == 1
            assert snapshot.execute("SELECT value FROM bot_settings WHERE key = 'submissions_paused'").fetchone()[0] == "0"
        await ctx.db.create_article("XG_BCDEFGHJ", 43, "new")
        await ctx.db.close()
        safety = restore_database(backup, ctx.db.path)
        assert safety and safety.exists()
        with closing(sqlite3.connect(safety)) as old:
            assert old.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2
        await ctx.db.open()
        assert await ctx.db.get_article("XG_ABCDEFGH")
        assert not await ctx.db.get_article("XG_BCDEFGHJ")
        assert await ctx.db.submissions_paused()
        assert not await ctx.db.get_pending_for_user(42)
        assert not await ctx.db.get_state(42)
    asyncio.run(scenario())


def test_restore_refuses_running_bot_and_lock_works_between_processes(ctx):
    backup = backup_database(ctx.db.path, ctx.db.path.parent / "backups")
    script = (
        "from pathlib import Path\nfrom app.backup import database_lock\nimport sys\n"
        "try:\n with database_lock(Path(sys.argv[1])): pass\n"
        "except RuntimeError:\n sys.exit(7)\n"
    )
    with database_lock(ctx.db.path):
        with pytest.raises(RuntimeError, match="停止服務"):
            restore_database(backup, ctx.db.path)
        child = subprocess.run([sys.executable, "-c", script, str(ctx.db.path)], capture_output=True, timeout=10)
        assert child.returncode == 7, child.stderr.decode(errors="replace")


def test_invalid_restore_does_not_replace_data(ctx, tmp_path):
    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "preserve")
        invalid = tmp_path / "invalid.sqlite3"
        invalid.write_bytes(b"not a sqlite database")
        with pytest.raises((sqlite3.DatabaseError, ValueError)):
            restore_database(invalid, ctx.db.path)
        assert await ctx.db.get_article("XG_ABCDEFGH")
        assert not list(tmp_path.glob(".restore-*"))
    asyncio.run(scenario())


def test_backup_and_restore_refuse_concurrent_backup_work(ctx):
    backup = backup_database(ctx.db.path, ctx.db.path.parent / "backups")
    with database_lock(ctx.db.path.with_name(ctx.db.path.name + ".backup")):
        with pytest.raises(RuntimeError):
            backup_database(ctx.db.path, ctx.db.path.parent / "backups")
        with pytest.raises(RuntimeError):
            restore_database(backup, ctx.db.path)
    assert not list(ctx.db.path.parent.glob(".restore-*"))


def test_failed_restore_replacement_keeps_checkpointed_current_data(ctx, monkeypatch):
    original_replace = os.replace

    def fail_restore(source, destination):
        if Path(destination) == ctx.db.path:
            raise OSError("simulated replacement failure")
        return original_replace(source, destination)

    async def scenario():
        await ctx.db.create_article("XG_ABCDEFGH", 42, "old")
        backup = backup_database(ctx.db.path, ctx.db.path.parent / "backups")
        await ctx.db.create_article("XG_BCDEFGHJ", 43, "new")
        await ctx.db.close()
        monkeypatch.setattr("app.backup.os.replace", fail_restore)
        with pytest.raises(OSError, match="replacement failure"):
            restore_database(backup, ctx.db.path)
        await ctx.db.open()
        assert await ctx.db.get_article("XG_ABCDEFGH")
        assert await ctx.db.get_article("XG_BCDEFGHJ")
        assert not list(ctx.db.path.parent.glob(".restore-*"))
    asyncio.run(scenario())
