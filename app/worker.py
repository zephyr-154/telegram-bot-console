from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

from app.backup import database_lock
from app.manager import BotManager
from app.config import read_dotenv
from app.storage import read_json, write_json


class PrivateLogFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [value for value in secrets if value]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for value in self.secrets:
            message = message.replace(value, "[隱藏]")
        if record.exc_info:
            message = f"執行錯誤 [{record.exc_info[0].__name__}]"
        record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = message, (), None, None, None
        return True


async def serve(manager: BotManager, name: str, nonce: str) -> None:
    folder = manager.folder(name)
    state_path = folder / "runtime.json"
    deadline = time.monotonic() + 10
    while read_json(state_path).get("nonce") != nonce:
        if time.monotonic() >= deadline:
            raise RuntimeError("未取得管理器的啟動紀錄")
        await asyncio.sleep(0.05)
    state = read_json(state_path)
    state.update(pid=os.getpid(), create_time=psutil.Process().create_time(), status="starting")
    write_json(state_path, state)
    values = read_dotenv(folder / ".env")
    handler = RotatingFileHandler(folder / "bot.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8")
    if os.name != "nt":
        (folder / "bot.log").chmod(0o600)
    handler.addFilter(PrivateLogFilter([values.get("BOT_TOKEN", ""), values.get("CONTENT_HASH_SECRET", "")]))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    old_handlers, old_level = root_logger.handlers[:], root_logger.level
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
    state["bot_id"] = values.get("BOT_TOKEN", "").split(":", 1)[0]
    state["chat_tracking"] = True
    write_json(state_path, state)

    def ready() -> None:
        state["status"] = "running"
        write_json(state_path, state)

    async def await_stop() -> None:
        while True:
            try:
                if (folder / "stop.request").read_text(encoding="utf-8") == nonce:
                    return
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.25)

    from app.main import run
    bot_task = asyncio.create_task(run(folder / ".env", ready_callback=ready, use_environment=False))
    stop_task = asyncio.create_task(await_stop())
    try:
        done, _ = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            state["status"] = "stopping"
            write_json(state_path, state)
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        else:
            await bot_task
        state["status"] = "stopped"
    except BaseException as exc:
        state.update(status="failed", error=type(exc).__name__)
        logging.getLogger(__name__).error("機器人已停止，錯誤類型：%s", type(exc).__name__)
        raise
    finally:
        stop_task.cancel()
        if not bot_task.done():
            bot_task.cancel()
        await asyncio.gather(bot_task, stop_task, return_exceptions=True)
        if read_json(state_path).get("nonce") == nonce:
            write_json(state_path, state)
        root_logger.handlers = old_handlers
        root_logger.setLevel(old_level)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tgbot 背景工作程序，由管理器啟動")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    manager = BotManager(args.root)
    folder = None
    try:
        folder = manager.folder(args.name)
        with database_lock(folder / "worker"):
            asyncio.run(serve(manager, args.name, args.nonce))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if folder is not None:
            state = read_json(folder / "runtime.json")
            if state.get("nonce") == args.nonce:
                state.update(status="failed", error=type(exc).__name__)
                write_json(folder / "runtime.json", state)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
