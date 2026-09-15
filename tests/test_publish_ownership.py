"""Published files and describarr's own folders must not come out root-owned.

describarr runs as root in its container. Until 2026-09-15 every published file
was root:root 644 and every `.describarr_backup` folder root 755, and three
library folders later blocked Radarr (uid 1000) from replacing a file on an
upgrade with "Failed to import movie". The replacement now inherits the
original's owner, group and mode; backup dirs and lock files inherit the
folder's owner and are group-writable.
"""
import os
import stat

import describarr.workflow as workflow
from describarr.workflow import _backup_original, _publish_in_place, _BACKUP_SUBDIR, _LIBRARY_DIR_MODE, _LIBRARY_FILE_MODE


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_published_file_keeps_the_original_mode(tmp_path):
    video = tmp_path / "Movie (2010).mkv"
    video.write_bytes(b"old" * 100)
    os.chmod(video, 0o640)
    combined = tmp_path / "ad_Movie (2010).mkv"
    combined.write_bytes(b"new" * 200)
    os.chmod(combined, 0o600)

    _publish_in_place(combined, video)

    assert video.read_bytes() == b"new" * 200
    assert _mode(video) == 0o640


def test_chown_refusal_does_not_block_publish(tmp_path, monkeypatch):
    video = tmp_path / "Movie (2010).mkv"
    video.write_bytes(b"old" * 100)
    combined = tmp_path / "ad_Movie (2010).mkv"
    combined.write_bytes(b"new" * 100)

    def refuse(*a, **k):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(workflow.os, "chown", refuse)

    _publish_in_place(combined, video)
    assert video.read_bytes() == b"new" * 100


def test_backup_dir_is_group_writable_setgid(tmp_path):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x" * 100)

    backup = _backup_original(video, None, 14)

    assert backup is not None
    assert _mode(tmp_path / _BACKUP_SUBDIR) == _LIBRARY_DIR_MODE


def test_lock_file_is_group_writable(tmp_path):
    video = tmp_path / "Movie (2010).mkv"
    video.write_bytes(b"old" * 100)
    combined = tmp_path / "ad_Movie (2010).mkv"
    combined.write_bytes(b"new" * 100)

    _publish_in_place(combined, video)

    lock = tmp_path / f".{video.name}.admerge.lock"
    assert lock.exists()
    assert _mode(lock) == _LIBRARY_FILE_MODE
