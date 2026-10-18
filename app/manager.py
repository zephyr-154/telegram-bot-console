from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil

from app.backup import database_lock
from app.config import ConfigurationError, read_dotenv, settings_from_values
from app.storage import atomic_write, read_json, write_json


ROOT_MARKER = ".tgbot-manager.json"
PROFILE_MARKER = "profile.json"


def default_manager_root() -> Path:
    if os.environ.get("TGBOT_HOME"):
        return Path(os.environ["TGBOT_HOME"]).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "TelegramAnonymousBoard"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tgbot"


def validate_name(name: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ConfigurationError("名稱不可包含控制字元")
    name = name.strip()
    reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)}
    if not re.fullmatch(r"[\w][\w-]{0,39}", name) or name.casefold() in reserved:
        raise ConfigurationError("名稱需為 1～40 個中英文字、數字、底線或連字號，不能使用系統保留名稱")
    return name


def safe_child(root: Path, path: Path) -> Path:
    if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
        raise ConfigurationError("路徑不在機器人專用目錄內，操作已停止")
    return path.resolve()


class BotManager:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_manager_root()).expanduser().absolute()
        self.children: dict[str, subprocess.Popen] = {}

    def ensure_root(self) -> None:
        forbidden = {Path(self.root.anchor), Path.home().resolve(), Path(__file__).resolve().parent.parent}
        if not (self.root / ROOT_MARKER).exists():
            forbidden.add(Path.cwd().resolve())
        if self.root.is_symlink() or self.root.is_junction() or self.root.resolve() in forbidden or self.root.resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ConfigurationError("請使用獨立的 TGBOT_HOME 資料夾，不可使用根目錄、家目錄或專案目錄")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.root / ROOT_MARKER
        if not marker.exists():
            write_json(marker, {"application": "telegram-anonymous-board", "id": uuid4().hex})
        if read_json(marker).get("application") != "telegram-anonymous-board":
            raise ConfigurationError("管理目錄標記無效")
        if not (self.root / ".gitignore").exists():
            atomic_write(self.root / ".gitignore", b"*\n")
        bots = self.root / "bots"
        if bots.exists():
            safe_child(self.root, bots)
        bots.mkdir(exist_ok=True, mode=0o700)

    def names(self) -> list[str]:
        if self.root.is_symlink() or self.root.is_junction():
            raise ConfigurationError("管理目錄不可為符號連結或接合點")
        folder = self.root / "bots"
        if not folder.is_dir():
            return []
        safe_child(self.root, folder)
        result = []
        for path in folder.iterdir():
            if path.is_dir() and not path.is_symlink() and not path.is_junction():
                profile = read_json(path / PROFILE_MARKER)
                if profile.get("name") == path.name and re.fullmatch(r"[a-f0-9]{32}", str(profile.get("id", ""))):
                    result.append(path.name)
        return sorted(result, key=str.casefold)

    def ensure_available(self) -> None:
        if (self.root / ".uninstalling").exists():
            raise ConfigurationError("正在解除安裝，請等待完成；失敗時請先查看解除安裝日誌")

    def folder(self, name: str) -> Path:
        if self.root.is_symlink() or self.root.is_junction():
            raise ConfigurationError("管理目錄不可為符號連結或接合點")
        name = validate_name(name)
        exact = self.root / "bots" / name
        if exact.is_dir() and read_json(exact / PROFILE_MARKER).get("name") == name:
            safe_child(self.root, self.root / "bots")
            return safe_child(self.root / "bots", exact)
        matches = [item for item in self.names() if item.casefold() == name.casefold()]
        if len(matches) != 1:
            available = "、".join(self.names()) or "尚無機器人，請使用 tgbot add"
            raise ConfigurationError(f"找不到唯一的機器人「{name}」。可用名稱：{available}")
        return safe_child(self.root / "bots", self.root / "bots" / matches[0])

    def create(self, name: str) -> Path:
        from app.settings_editor import initial_values, render_env
        name = validate_name(name)
        self.ensure_root()
        with database_lock(self.root / "manager"):
            self.ensure_available()
            if any(item.casefold() == name.casefold() for item in self.names()):
                raise ConfigurationError("這個機器人名稱已經存在")
            folder = self.root / "bots" / name
            folder.mkdir(mode=0o700)
            try:
                write_json(folder / PROFILE_MARKER, {"name": name, "id": uuid4().hex})
                atomic_write(folder / ".env", render_env(None, initial_values(folder / ".env")))
            except BaseException:
                for file in folder.iterdir():
                    file.unlink()
                folder.rmdir()
                raise
        return folder

    def validate_values(self, name: str, values: dict[str, str]) -> None:
        from app.settings_editor import validate_values
        folder = self.folder(name)
        validate_values(values, folder)
        settings = settings_from_values(values, folder)
        for path in (settings.database_path, settings.temp_dir):
            safe_child(folder, path)
        identity = values["BOT_TOKEN"].split(":", 1)[0]
        for other in self.names():
            if other.casefold() != name.casefold():
                token = read_dotenv(self.folder(other) / ".env").get("BOT_TOKEN", "")
                active_identity = read_json(self.folder(other) / "runtime.json").get("bot_id") if self.process(other) else None
                if (token and token.split(":", 1)[0] == identity) or active_identity == identity:
                    raise ConfigurationError(f"這個 Telegram Bot 已經用在「{other}」，不能重複部署")

    def save(self, name: str, values: dict[str, str], original: bytes) -> bool:
        from app.settings_editor import save_config
        self.ensure_root()
        with database_lock(self.root / "manager"):
            self.ensure_available()
            self.validate_values(name, values)
            return save_config(self.folder(name) / ".env", values, original)

    def process(self, name: str) -> psutil.Process | None:
        folder = self.folder(name)
        state = read_json(folder / "runtime.json")
        child = self.children.get(folder.name)
        if child is not None and child.poll() is not None:
            self.children.pop(folder.name, None)
        try:
            process = psutil.Process(int(state["pid"]))
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            if abs(process.create_time() - float(state["create_time"])) > 0.01:
                return None
            command = process.cmdline()
            signature = ["-m", "app.worker", "--root", str(self.root.resolve()), "--name", folder.name, "--nonce", state["nonce"]]
            if command[1:] != signature:
                return None
            return process
        except (psutil.NoSuchProcess, KeyError, ValueError, TypeError):
            return None
        except psutil.AccessDenied:
            raise ConfigurationError(f"無法核對「{folder.name}」的程序權限，請使用啟動它的帳號") from None

    def status(self, name: str) -> dict:
        folder = self.folder(name)
        state = read_json(folder / "runtime.json")
        process = self.process(name)
        if process is not None:
            try:
                status = state.get("status", "starting")
                if status == "stopped":
                    status = "stopping"
                return {"name": folder.name, "status": status, "pid": process.pid,
                        "memory_mb": process.memory_info().rss / 1024**2, "uptime": max(0, time.time() - process.create_time())}
            except psutil.NoSuchProcess:
                pass
        status = "failed" if state and state.get("status") not in {"stopped", "new"} else "stopped"
        if not state:
            try:
                self.validate_values(name, read_dotenv(folder / ".env"))
            except ConfigurationError:
                status = "unconfigured"
        return {"name": folder.name, "status": status, "pid": None, "memory_mb": 0, "uptime": 0,
                "error": state.get("error", "")}

    def start(self, name: str) -> str:
        self.ensure_root()
        with database_lock(self.root / "manager"):
            self.ensure_available()
            folder = self.folder(name)
            if self.process(name) is not None:
                return "已在執行"
            with database_lock(folder / "worker"):
                pass
            values = read_dotenv(folder / ".env")
            self.validate_values(name, values)
            nonce = uuid4().hex
            (folder / "stop.request").unlink(missing_ok=True)
            env = os.environ.copy()
            from app.settings_editor import DEFAULTS
            for key in (*DEFAULTS, "TGBOT_CONFIG"):
                env.pop(key, None)
            env["PYTHONUTF8"] = "1"
            source = Path(__file__).resolve().parent.parent
            if (source / "tgbot.cmd").is_file():
                env["PYTHONPATH"] = str(source) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command = [sys.executable, "-m", "app.worker", "--root", str(self.root.resolve()), "--name", folder.name, "--nonce", nonce]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=folder, env=env, close_fds=True, **options)
            self.children[folder.name] = child
            try:
                created = psutil.Process(child.pid).create_time()
                write_json(folder / "runtime.json", {"pid": child.pid, "create_time": created, "nonce": nonce,
                                                     "status": "starting", "started_at": time.time(), "bot_id": values["BOT_TOKEN"].split(":", 1)[0]})
            except BaseException:
                child.kill()
                child.wait(timeout=5)
                raise
        return "啟動中（背景）"

    def stop(self, name: str, timeout: float = 15) -> str:
        self.ensure_root()
        with database_lock(self.root / "manager"):
            folder = self.folder(name)
            process = self.process(name)
            if process is None:
                with database_lock(folder / "worker"):
                    pass
                return "已停止"
            state = read_json(folder / "runtime.json")
            atomic_write(folder / "stop.request", state["nonce"].encode())
            try:
                process.wait(timeout=timeout)
            except psutil.TimeoutExpired:
                current = self.process(name)
                if current is not None:
                    current.kill()
                    try:
                        current.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        raise ConfigurationError(f"「{name}」無法停止，未刪除任何資料") from None
            child = self.children.pop(folder.name, None)
            if child is not None:
                child.wait(timeout=5)
            state["status"] = "stopped"
            write_json(folder / "runtime.json", state)
        return "已停止"

    def delete(self, name: str) -> None:
        self.stop(name)
        with database_lock(self.root / "manager"):
            self.ensure_available()
            folder = self.folder(name)
            if self.process(name) is not None:
                raise ConfigurationError("機器人重新啟動中，請先停止後再刪除")
            # Only remove a marked, resolved child of this manager's bots directory.
            safe_child(self.root / "bots", folder)
            shutil.rmtree(folder)

    def all(self, action: str) -> bool:
        success = True
        names = self.names()
        failed = set()
        deadline = time.monotonic() + 15
        if action == "stop" and names:
            with database_lock(self.root / "manager"):
                for name in names:
                    try:
                        if self.process(name) is not None:
                            folder = self.folder(name)
                            nonce = read_json(folder / "runtime.json")["nonce"]
                            atomic_write(folder / "stop.request", nonce.encode())
                    except (ConfigurationError, OSError, RuntimeError) as exc:
                        success = False
                        failed.add(name)
                        print(f"{name}：{exc}")
        for name in names:
            if name in failed:
                continue
            try:
                if action == "restart":
                    self.stop(name)
                    result = self.start(name)
                elif action == "stop":
                    result = self.stop(name, timeout=max(0, deadline - time.monotonic()))
                else:
                    result = getattr(self, action)(name)
                print(f"{name}：{result}")
            except (ConfigurationError, OSError, RuntimeError) as exc:
                success = False
                print(f"{name}：{exc}")
        if not self.names():
            print("尚無機器人，請使用 tgbot add 新增。")
        return success
