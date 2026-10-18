from __future__ import annotations

import pytest

from app import storage


def test_atomic_write_retries_temporary_windows_sharing_conflicts(tmp_path, monkeypatch):
    path = tmp_path / "stop.request"
    path.write_bytes(b"old")
    replace = storage.os.replace
    attempts = []
    def busy_once(source, target):
        attempts.append(target)
        if len(attempts) == 1:
            error = PermissionError("File is being read")
            error.winerror = 32
            raise error
        replace(source, target)
    monkeypatch.setattr(storage.os, "replace", busy_once)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    storage.atomic_write(path, b"new")
    assert path.read_bytes() == b"new" and len(attempts) == 2
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 6), (32, 6), (None, 1)])
def test_atomic_write_retains_original_on_persistent_failure(tmp_path, monkeypatch, winerror, expected_attempts):
    path = tmp_path / "stop.request"
    path.write_bytes(b"old")
    attempts = []
    def fail(source, target):
        attempts.append(target)
        error = PermissionError("Access denied")
        error.winerror = winerror
        raise error
    monkeypatch.setattr(storage.os, "replace", fail)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError):
        storage.atomic_write(path, b"new")
    assert path.read_bytes() == b"old" and len(attempts) == expected_attempts
    assert not list(tmp_path.glob("*.tmp"))
