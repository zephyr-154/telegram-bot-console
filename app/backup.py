from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import load_settings


@contextmanager
def database_lock(database: Path):
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = database.with_suffix(database.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("另一個機器人、備份或還原程序正在使用資料庫，請稍後重試；還原前請先停止服務。") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("資料庫完整性檢查失敗。")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"articles", "article_messages", "pending_submissions", "user_states", "blocked_users"} <= tables:
        raise ValueError("這不是此機器人的資料庫備份。")


def backup_database(database: Path, directory: Path, keep: int = 7) -> Path:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError("找不到機器人資料庫。")
    if keep < 1:
        raise ValueError("至少必須保留一份備份。")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"bot-backup-{stamp}-{uuid4().hex[:8]}.sqlite3"
    fd, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=directory)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with database_lock(database.with_name(database.name + ".backup")):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(temporary_path)) as target:
                    source.backup(target)
                    _check_database(target)
            os.replace(temporary_path, destination)
            backups = sorted(
                (path for path in directory.glob("bot-backup-*.sqlite3") if path.resolve() != database),
                reverse=True,
            )
            for old in backups[keep:]:
                old.unlink()
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def restore_database(backup: Path, database: Path) -> Path | None:
    backup, database = backup.resolve(), database.resolve()
    if backup == database:
        raise ValueError("備份與還原目標不可為同一個檔案。")
    if not backup.is_file():
        raise FileNotFoundError("找不到指定的備份。")
    with database_lock(database):
        fd, temporary = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite3", dir=database.parent)
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as source:
                _check_database(source)
                with closing(sqlite3.connect(temporary_path)) as target:
                    source.backup(target)
                    _check_database(target)
                    target.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    target.execute(
                        "INSERT INTO bot_settings(key, value) VALUES ('submissions_paused', '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    target.execute("DELETE FROM pending_submissions")
                    target.execute("DELETE FROM user_states")
                    target.commit()
            safety_backup = None
            if database.exists():
                safety_backup = backup_database(database, database.parent / "backups")
            with database_lock(database.with_name(database.name + ".backup")):
                if database.exists():
                    with closing(sqlite3.connect(database)) as current:
                        checkpoint = current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if checkpoint[0] != 0:
                            raise RuntimeError("資料庫仍有其他連線，還原已停止，原資料未覆寫。")
                    # Checkpoint first so a failed replacement cannot lose WAL data.
                    Path(str(database) + "-wal").unlink(missing_ok=True)
                    Path(str(database) + "-shm").unlink(missing_ok=True)
                os.replace(temporary_path, database)
            if os.name != "nt":
                database.chmod(0o600)
            return safety_backup
        finally:
            temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None, *, env_path: str | Path | None = None, use_environment: bool = True) -> None:
    parser = argparse.ArgumentParser(description="機器人資料庫備份／還原；備份包含作者 ID 等私人資料。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="建立一致的 SQLite 備份，保留最近 7 份")
    restore = commands.add_parser("restore", help="停止機器人後還原備份；先自動備份目前資料")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings(env_path, use_environment=use_environment)
    if args.command == "create":
        print(backup_database(settings.database_path, settings.database_path.parent / "backups"))
    else:
        safety = restore_database(args.backup, settings.database_path)
        print(f"還原完成，投稿已暫停。請先核對備份時點之後的 Telegram 貼文，再由管理員恢復投稿。還原前備份：{safety or '原資料庫不存在'}")


if __name__ == "__main__":
    main()
