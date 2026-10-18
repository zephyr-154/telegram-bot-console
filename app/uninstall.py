"""Standalone uninstall helper; kept independent of the installed package."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path


PACKAGE = "telegram-anonymous-board"
MARKER = ".tgbot-manager.json"


def validate_removal_root(root: Path, identity: str) -> Path:
    resolved = root.resolve()
    if root.is_symlink() or root.is_junction() or resolved in {Path(root.anchor), Path.home().resolve(), Path.cwd().resolve()}:
        raise ValueError("拒絕刪除不安全的管理目錄")
    marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if marker.get("application") != PACKAGE or marker.get("id") != identity:
        raise ValueError("管理目錄身分已變更，停止刪除")
    return resolved


def wait_for_parent(pid: int, timeout: float = 40) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                    raise RuntimeError("CLI 尚未結束，未解除安裝")
            finally:
                kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("CLI 尚未結束，未解除安裝")
            time.sleep(0.1)


def remove_installation(plan: dict) -> None:
    root = validate_removal_root(Path(plan["root"]), plan["identity"])
    wait_for_parent(plan["parent_pid"])
    result = subprocess.run(plan["command"], capture_output=True, timeout=120)
    if result.returncode != 0:
        (root / ".uninstalling").unlink(missing_ok=True)
        raise RuntimeError("解除套件安裝失敗，機器人資料已保留")
    # Validate the absolute target and its ownership marker again immediately before removal.
    root = validate_removal_root(root, plan["identity"])
    shutil.rmtree(root)
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
                user_path, value_type = winreg.QueryValueEx(key, "Path")
                target = os.path.normcase(str(root / "bin"))
                parts = [part for part in user_path.split(";") if os.path.normcase(os.path.normpath(os.path.expandvars(part.strip().strip('"')))) != target]
                updated = ";".join(parts)
                if updated != user_path:
                    winreg.SetValueEx(key, "Path", 0, value_type, updated)
        except OSError:
            print("程式與資料已移除；舊捷徑的使用者 PATH 項目請另行檢查。")


def schedule_uninstall(manager) -> Path:
    receipt_path = Path(sys.prefix) / "uv-receipt.toml"
    if not receipt_path.is_file():
        raise ValueError("完整解除安裝適用 uv 套件版；請使用 uv tool install 安裝後執行此指令。原始碼專案不會被自動刪除")
    receipt = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
    if not any(item.get("name") == PACKAGE for item in receipt.get("tool", {}).get("requirements", [])):
        raise ValueError("無法確認安裝套件身分，未刪除資料")
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("找不到 uv，請先確認 uv 指令可用")
    manager.ensure_root()
    marker = json.loads((manager.root / MARKER).read_text(encoding="utf-8"))
    validate_removal_root(manager.root, marker["id"])
    base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if base_python.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("找不到獨立的 Python 解除安裝程序，未刪除資料")
    from app.backup import database_lock
    from app.storage import atomic_write
    with database_lock(manager.root / "manager"):
        manager.ensure_available()
        atomic_write(manager.root / ".uninstalling", marker["id"].encode())
    try:
        if not manager.all("stop"):
            raise ValueError("有機器人無法停止，未解除安裝")
        import psutil
        deadline = time.monotonic() + 3
        signature = ["-m", "app.worker", "--root", str(manager.root.resolve())]
        while any((process.info["cmdline"] or [])[1:5] == signature for process in psutil.process_iter(["cmdline"])):
            if time.monotonic() >= deadline:
                raise ValueError("仍有無法核對的背景程序，未解除安裝；請先檢查執行紀錄")
            time.sleep(.1)
    except BaseException:
        (manager.root / ".uninstalling").unlink(missing_ok=True)
        raise
    try:
        temporary = Path(tempfile.mkdtemp(prefix="tgbot-uninstall-"))
        helper = temporary / "uninstall.py"
        helper.write_bytes(Path(__file__).read_bytes())
        plan = temporary / "plan.json"
        plan.write_text(json.dumps({"root": str(manager.root.resolve()), "identity": marker["id"], "parent_pid": os.getpid(),
                                    "command": [uv, "tool", "uninstall", PACKAGE]}), encoding="utf-8")
        log = temporary / "uninstall.log"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with log.open("wb") as output:
            subprocess.Popen([str(base_python), "-I", "-X", "utf8", str(helper), str(plan)], stdin=subprocess.DEVNULL,
                             stdout=output, stderr=subprocess.STDOUT, cwd=temporary, close_fds=True, **options)
        return log
    except BaseException:
        validate_removal_root(manager.root, marker["id"])
        (manager.root / ".uninstalling").unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        remove_installation(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
        print("已移除 tgbot 套件、指令、全部機器人與私人資料。")
    except Exception as exc:
        print(f"解除安裝未完成：{exc}")
        raise SystemExit(1)
    finally:
        helper = Path(__file__).resolve()
        if helper.parent.name.startswith("tgbot-uninstall-") and helper.parent.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            (helper.parent / "plan.json").unlink(missing_ok=True)
            helper.unlink(missing_ok=True)
