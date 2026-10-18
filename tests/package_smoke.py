"""Offline smoke test: isolated uv installation, real worker lifecycle, complete uninstall.

Run after `uv build`: python tests/package_smoke.py
The installed worker uses an injected local application stub; Telegram is never contacted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import __version__
from app.config import read_dotenv
from app.manager import BotManager
from app.settings_editor import save_config
from app.storage import read_json


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("Isolated package did not reach the expected state")


def main():
    uv = shutil.which("uv")
    assert uv, "uv is required"
    wheel = ROOT / "dist" / f"telegram_anonymous_board-{__version__}-py3-none-any.whl"
    assert wheel.is_file(), "Run uv build first"
    with tempfile.TemporaryDirectory(prefix="tgbot-package-smoke-") as temporary:
        base = Path(temporary)
        env = os.environ.copy()
        env.update(UV_TOOL_DIR=str(base / "tools"), UV_TOOL_BIN_DIR=str(base / "bin"),
                   TGBOT_HOME=str(base / "data"), PYTHONUTF8="1", UV_NO_PROGRESS="1")
        result = subprocess.run([uv, "tool", "install", "--python", sys.executable, str(wheel)],
                                env=env, capture_output=True, encoding="utf-8", timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        executable = base / "bin" / ("tgbot.exe" if os.name == "nt" else "tgbot")
        installed = base / "tools" / "telegram-anonymous-board"
        site = next(installed.rglob("site-packages/app/worker.py")).parent
        stub = base / "stub" / "app"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text(f"__path__ = [{str(stub)!r}, {str(site)!r}]\n__version__ = {__version__!r}\n", encoding="utf-8")
        (stub / "main.py").write_text(
            "import asyncio\nasync def run(env_path=None, *, ready_callback=None, use_environment=True):\n"
            "    assert use_environment is False\n    ready_callback()\n    await asyncio.sleep(10000)\n", encoding="utf-8")
        env["PYTHONPATH"] = str(stub.parent)
        for key in ("TGBOT_CONFIG", "BOT_TOKEN", "CHANNEL_ID", "ADMIN_USER_ID"):
            env.pop(key, None)
        unrelated = base / "unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("must remain")
        manager = BotManager(base / "data")

        def command(*args, input=None):
            result = subprocess.run([str(executable), *args], cwd=unrelated, env=env,
                                    input=input, capture_output=True, encoding="utf-8", timeout=45)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout

        try:
            assert command("--version").strip() == f"tgbot {__version__}"
            command("add", "first", "--no-config")
            command("add", "second", "--no-config")
            for name, bot_id in (("first", "9988776655"), ("second", "8877665544")):
                folder = manager.folder(name)
                font = folder / "offline-font.ttf"
                font.touch()
                values = read_dotenv(folder / ".env")
                values.update(BOT_TOKEN=f"{bot_id}:offline_test_only", CHANNEL_ID="-100123", ADMIN_USER_ID="42",
                              FONT_REGULAR_PATH=str(font), FONT_BOLD_PATH=str(font))
                save_config(folder / ".env", values, (folder / ".env").read_bytes())
            command("start")
            wait_for(lambda: all(manager.status(name)["status"] == "running" for name in manager.names()))
            assert "執行中" in command("status")
            second_pid = manager.process("second").pid
            shell_output = command(input="tgbot status\ntgbot names\nhelp\nexit\n")
            assert "first" in shell_output and "second" in shell_output and "Ready>" in shell_output
            assert manager.process("second").pid == second_pid
            command("stop", "first")
            assert manager.process("first") is None and manager.process("second") is not None
            command("restart")
            wait_for(lambda: all(manager.status(name)["status"] == "running" for name in manager.names()))
            assert manager.process("second").pid != second_pid
            command("delete", "first", "--yes")
            assert manager.names() == ["second"] and manager.process("second") is not None
            output = command("app", "delete", "--yes")
            log = Path(output.split("結果日誌：", 1)[1].strip())
            try:
                wait_for(lambda: not manager.root.exists() and not executable.exists() and not installed.exists(), timeout=45)
            except AssertionError:
                raise AssertionError(log.read_text(encoding="utf-8") if log.exists() else output) from None
            assert (unrelated / "keep.txt").read_text() == "must remain"
            wait_for(lambda: not (log.parent / "plan.json").exists() and not (log.parent / "uninstall.py").exists())
            print("Isolated package passed: two real background workers, restart, selected delete and full uninstall.")
            print("Terminal exit preserved workers; uninstall removed only its own tool environment and managed data.")
            print("No Telegram connection was made.")
        finally:
            if manager.root.exists():
                manager.all("stop")


if __name__ == "__main__":
    main()
