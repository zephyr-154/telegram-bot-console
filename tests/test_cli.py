from __future__ import annotations

import getpass
import asyncio
import os
import shutil
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import __version__, cli, config
from app.config import ConfigurationError, default_env_path, load_settings, read_dotenv


def test_macos_fonts_are_detected_without_overwriting_custom_paths(tmp_path, monkeypatch):
    from app import settings_editor
    monkeypatch.setattr(settings_editor.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fonts = tmp_path / "Library" / "Fonts"
    fonts.mkdir(parents=True)
    regular = fonts / "NotoSansCJKtc-Regular.otf"
    bold = fonts / "NotoSansCJKtc-Bold.otf"
    regular.touch()
    bold.touch()
    path = tmp_path / "profile" / ".env"
    values = cli.initial_values(path)
    assert values["FONT_REGULAR_PATH"] == str(regular)
    assert values["FONT_BOLD_PATH"] == str(bold)
    path.parent.mkdir()
    path.write_text("FONT_REGULAR_PATH=/custom/font.otf\n", encoding="utf-8")
    assert cli.initial_values(path)["FONT_REGULAR_PATH"] == "/custom/font.otf"


@pytest.fixture
def configured(tmp_path):
    fonts = tmp_path / "assets" / "fonts"
    fonts.mkdir(parents=True)
    for name in ("NotoSansTC-Regular.otf", "NotoSansTC-Bold.otf"):
        (fonts / name).touch()
    path = tmp_path / ".env"
    values = cli.initial_values(path)
    values.update(BOT_TOKEN="9876543210:offline_test_token", CHANNEL_ID="-100246810", ADMIN_USER_ID="42")
    return path, values


def test_load_settings_refreshes_file_without_polluting_environment(configured, monkeypatch):
    path, values = configured
    for key in cli.DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    cli.save_config(path, values, None)
    assert load_settings(path).channel_id == -100246810
    assert "BOT_TOKEN" not in os.environ
    values["CHANNEL_ID"] = "-1009988"
    cli.save_config(path, values, path.read_bytes())
    assert load_settings(path).channel_id == -1009988
    monkeypatch.setenv("CHANNEL_ID", "-1005566")
    assert load_settings(path).channel_id == -1005566
    assert read_dotenv(path)["CHANNEL_ID"] == "-1009988"


def test_dotenv_supports_bom_and_quoted_paths_and_first_value_wins(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\nPATH="a b/中文字型.otf"\nX=first\nX=second\nSECRET=abc=def\n', encoding="utf-8-sig")
    assert read_dotenv(path) == {"PATH": "a b/中文字型.otf", "X": "first", "SECRET": "abc=def"}


def test_first_save_generates_secret_without_backup_or_database(configured):
    path, values = configured
    assert len(values["CONTENT_HASH_SECRET"]) == 64
    assert cli.save_config(path, values, None)
    assert read_dotenv(path) == values
    assert not path.with_name(".env.bak").exists()
    assert not (path.parent / "data").exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_preserves_secret_comments_unknown_settings_and_exact_previous_file(configured):
    path, values = configured
    original = b"\xef\xbb\xbf# private config\r\nEXTRA='keep me'\r\n" + cli.render_env(None, values) + b"CHANNEL_ID=-1001\n"
    path.write_bytes(original)
    edited = cli.initial_values(path)
    edited["ADMIN_USER_ID"] = "84"
    assert cli.save_config(path, edited, original)
    assert path.with_name(".env.bak").read_bytes() == original
    assert "# private config\nEXTRA='keep me'\n" in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").count("CHANNEL_ID=") == 1
    assert read_dotenv(path)["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"]
    assert read_dotenv(path)["ADMIN_USER_ID"] == "84"
    backup = path.with_name(".env.bak").read_bytes()
    assert not cli.save_config(path, edited, path.read_bytes())
    assert path.with_name(".env.bak").read_bytes() == backup


def test_concurrent_file_edit_is_not_overwritten(configured):
    path, values = configured
    path.write_bytes(b"# another terminal saved this\n")
    with pytest.raises(ConfigurationError, match="其他程式"):
        cli.save_config(path, values, None)
    assert path.read_bytes() == b"# another terminal saved this\n"
    assert not path.with_name(".env.bak").exists()


def test_atomic_save_failure_preserves_original_and_cleans_temporary_file(configured, monkeypatch):
    path, values = configured
    cli.save_config(path, values, None)
    original = path.read_bytes()
    values["ADMIN_USER_ID"] = "84"
    replace = os.replace
    def fail_config_replace(source, destination):
        if Path(destination) == path:
            raise PermissionError("test failure")
        replace(source, destination)
    monkeypatch.setattr(cli.os, "replace", fail_config_replace)
    with pytest.raises(PermissionError):
        cli.save_config(path, values, original)
    assert path.read_bytes() == original
    assert path.with_name(".env.bak").read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("key,value", [
    ("BOT_TOKEN", "sensitive-invalid-token"),
    ("BOT_TOKEN", "123456789:replace_me"),
    ("BOT_TOKEN", "9876543210:abc\nADMIN_USER_ID=123"),
    ("CHANNEL_ID", "100123"), ("CHANNEL_ID", "0"), ("CHANNEL_ID", "abc"),
    ("ADMIN_USER_ID", "-42"), ("TIMEZONE", "../bad"),
    ("FONT_REGULAR_PATH", "missing.otf"),
])
def test_invalid_settings_are_not_saved_or_exposed(configured, key, value):
    path, values = configured
    values[key] = value
    with pytest.raises(ConfigurationError) as caught:
        cli.save_config(path, values, None)
    if key == "BOT_TOKEN":
        assert value not in str(caught.value)
    assert not path.exists()


def test_interactive_first_configuration_saves_without_echoing_token(configured, monkeypatch, capsys):
    path, values = configured
    choices = iter(["1", "2", values["CHANNEL_ID"], "3", values["ADMIN_USER_ID"], "7", "8"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: values["BOT_TOKEN"])
    assert cli.main(["--config", str(path)]) == 0
    output = capsys.readouterr().out
    assert values["BOT_TOKEN"] not in output
    assert "已儲存" in output
    saved = read_dotenv(path)
    assert saved["BOT_TOKEN"] == values["BOT_TOKEN"]
    assert saved["ADMIN_USER_ID"] == values["ADMIN_USER_ID"]
    assert len(saved["CONTENT_HASH_SECRET"]) == 64


def test_leaving_or_interrupting_menu_does_not_create_config(configured, monkeypatch):
    path, _ = configured
    monkeypatch.setattr("builtins.input", lambda prompt: "0")
    assert cli.main(["--config", str(path)]) == 0
    assert not path.exists()
    def interrupt(prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    assert cli.main(["--config", str(path)]) == 0
    assert not path.exists()


def test_declining_to_discard_changes_returns_to_menu(configured, monkeypatch):
    path, _ = configured
    choices = iter(["3", "42", "0", "n", "0", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    assert cli.main(["--config", str(path)]) == 0
    assert not path.exists()


def test_secret_input_refuses_unhidden_fallback(configured, monkeypatch):
    path, values = configured
    def unsafe_terminal(prompt):
        warnings.warn("cannot hide input", getpass.GetPassWarning)
        pytest.fail("must not fall back to echoed input")
    monkeypatch.setattr(cli.getpass, "getpass", unsafe_terminal)
    with pytest.raises(ConfigurationError, match="無法隱藏"):
        cli.edit_field(values, "BOT_TOKEN", path.parent)


def test_check_uses_file_values_and_does_not_write_or_connect(configured, monkeypatch, capsys):
    path, values = configured
    assert cli.main(["--config", str(path), "--check"]) == 1
    cli.save_config(path, values, None)
    original = path.read_bytes()
    monkeypatch.setenv("BOT_TOKEN", "invalid-external-token")
    assert cli.main(["--config", str(path), "--check"]) == 0
    assert path.read_bytes() == original
    assert not path.with_name(".env.bak").exists()
    assert values["BOT_TOKEN"] not in capsys.readouterr().out


def test_windows_install_is_idempotent_preserves_user_path_and_refuses_other_project(tmp_path, monkeypatch):
    class RegistryKey:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    registry = {"Path": (r"%USERPROFILE%\tools;C:\existing", 2)}
    def set_value(key, name, reserved, value_type, value):
        registry[name] = value, value_type
    winreg = SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_EXPAND_SZ=2,
        CreateKeyEx=lambda *args: RegistryKey(), QueryValueEx=lambda key, name: registry[name],
        SetValueEx=set_value,
    )
    ctypes = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(SendMessageTimeoutW=lambda *args: 1)))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setitem(sys.modules, "ctypes", ctypes)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(cli, "PROJECT_DIR", tmp_path / "專案 %test%")
    cli.PROJECT_DIR.mkdir()
    (cli.PROJECT_DIR / "tgbot.cmd").touch()
    command = cli.install_windows_command()
    first_path = registry["Path"]
    assert first_path[0].startswith(r"%USERPROFILE%\tools;C:\existing;")
    assert command.parent.name == "bin"
    assert "%%test%%" in command.read_text(encoding="utf-8")
    assert cli.install_windows_command() == command
    assert registry["Path"] == first_path
    monkeypatch.setattr(cli, "PROJECT_DIR", tmp_path / "other")
    cli.PROJECT_DIR.mkdir()
    (cli.PROJECT_DIR / "tgbot.cmd").touch()
    with pytest.raises(ConfigurationError, match="另一份專案"):
        cli.install_windows_command()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CMD launcher")
def test_cmd_launcher_works_from_another_folder_and_propagates_failure(configured):
    path, _ = configured
    launcher = cli.PROJECT_DIR / "tgbot.cmd"
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    def run(*args):
        return subprocess.run(
            ["cmd.exe", "/d", "/c", str(launcher), *args], cwd=path.parent,
            env=env, capture_output=True, encoding="utf-8", timeout=20,
        )
    help_result = run("--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "--check" in help_result.stdout
    missing_result = run("--check", "--config", str(path))
    assert missing_result.returncode == 1
    assert "尚未建立設定檔" in missing_result.stderr


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_installed_config_lives_in_user_folder_independent_of_working_directory(tmp_path, monkeypatch, platform):
    monkeypatch.delenv("TGBOT_CONFIG", raising=False)
    monkeypatch.setattr(config, "__file__", str(tmp_path / "site-packages" / "app" / "config.py"))
    monkeypatch.setattr(config.sys, "platform", platform)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    expected = tmp_path / "local" / "TelegramAnonymousBoard" / ".env" if platform == "win32" else tmp_path / "config" / "tgbot" / ".env"
    assert default_env_path() == expected
    monkeypatch.chdir(tmp_path)
    assert default_env_path() == expected
    assert not expected.parent.exists()


def test_source_config_and_explicit_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("TGBOT_CONFIG", raising=False)
    assert default_env_path() == cli.PROJECT_DIR / ".env"
    selected = tmp_path / "private" / ".env"
    monkeypatch.setenv("TGBOT_CONFIG", str(selected))
    assert default_env_path() == selected


def test_first_save_creates_private_user_config_folder(configured):
    original_path, values = configured
    path = original_path.parent / "new-user" / "settings" / ".env"
    assert cli.save_config(path, values, None)
    assert read_dotenv(path)["CONTENT_HASH_SECRET"] == values["CONTENT_HASH_SECRET"]
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_cli_commands_forward_selected_config_without_changing_working_directory(configured, monkeypatch):
    import app.main
    import app.backup
    path, values = configured
    cli.save_config(path, values, None)
    calls = []
    cwd = Path.cwd()
    monkeypatch.setattr(app.main, "main", lambda **kwargs: calls.append(("start", kwargs)))
    monkeypatch.setattr(app.backup, "main", lambda argv, **kwargs: calls.append((argv, kwargs)))
    assert cli.main(["run", "--config", str(path)]) == 0
    assert cli.main(["backup", "--config", str(path)]) == 0
    backup_file = path.parent / "my backups" / "snapshot.sqlite3"
    assert cli.main(["--config", str(path), "restore", str(backup_file)]) == 0
    assert calls == [
        ("start", {"env_path": path}),
        (["create"], {"env_path": path}),
        (["restore", str(backup_file)], {"env_path": path}),
    ]
    assert Path.cwd() == cwd
    assert cli.main(["check", "--config", str(path)]) == 0


def test_cli_backup_and_restore_use_same_database_as_runtime(configured, ctx):
    path, values = configured
    values["DATABASE_PATH"] = str(ctx.db.path)
    cli.save_config(path, values, None)
    asyncio.run(ctx.db.create_article("XG_ABCDEFGH", 42, "digest"))
    assert cli.main(["backup", "--config", str(path)]) == 0
    backup_file, = (ctx.db.path.parent / "backups").glob("*.sqlite3")
    asyncio.run(ctx.db.create_article("XG_BCDEFGHJ", 43, "new"))
    asyncio.run(ctx.db.close())
    assert cli.main(["restore", str(backup_file), "--config", str(path)]) == 0
    asyncio.run(ctx.db.open())
    assert asyncio.run(ctx.db.get_article("XG_ABCDEFGH"))
    assert not asyncio.run(ctx.db.get_article("XG_BCDEFGHJ"))
    assert asyncio.run(ctx.db.submissions_paused())


@pytest.mark.parametrize("args", [["restore"], ["start", "one", "extra"], ["start", "--check"], ["restore", "backup.sqlite3", "--install"], ["restore", "bot", "backup.sqlite3", "--config", "settings.env"]])
def test_cli_rejects_ambiguous_or_incomplete_commands(args):
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2


def test_start_reports_token_error_without_echoing_token(configured, monkeypatch, capsys):
    import app.main
    path, values = configured
    def invalid_token(**kwargs):
        raise cli.TokenValidationError(values["BOT_TOKEN"])
    monkeypatch.setattr(app.main, "main", invalid_token)
    assert cli.main(["run", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert values["BOT_TOKEN"] not in output.out + output.err
    assert "Token 格式錯誤" in output.err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CMD launcher")
def test_source_launcher_delegates_to_installed_executable(tmp_path):
    folder = tmp_path / "installed bin"
    folder.mkdir()
    shutil.copyfile(os.environ["COMSPEC"], folder / "tgbot.exe")
    env = os.environ.copy()
    env["PATH"] = str(folder) + ";" + str(Path(os.environ["SYSTEMROOT"]) / "System32")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(cli.PROJECT_DIR / "tgbot.cmd"), "/d", "/c", "exit", "17"],
        cwd=tmp_path, env=env, capture_output=True, timeout=20,
    )
    assert result.returncode == 17, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CMD launcher")
def test_source_launcher_falls_back_to_local_venv(tmp_path):
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(cli.PROJECT_DIR / "tgbot.cmd"), "--version"],
        cwd=tmp_path, env=env, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"tgbot {__version__}".encode()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows redirected output encoding")
def test_cli_redirected_chinese_output_uses_utf8_even_with_cp950_environment():
    env = os.environ.copy()
    env.update(PYTHONUTF8="0", PYTHONIOENCODING="cp950")
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"], cwd=cli.PROJECT_DIR, env=env,
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "多機器人" in result.stdout.decode("utf-8")
