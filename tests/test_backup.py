"""Backup-before-overwrite (hardlink) and retention pruning."""

import os
import time

from describarr.workflow import _backup_original, _prune_old_backups, _BACKUP_SUBDIR


def test_backup_hardlinks_into_sibling_dir(tmp_path):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x" * 100)

    backup = _backup_original(video, None, 14)

    assert backup is not None and backup.exists()
    assert backup.parent == tmp_path / _BACKUP_SUBDIR
    # Hardlink → same inode as the original, original untouched.
    assert backup.stat().st_ino == video.stat().st_ino
    assert video.exists()


def test_backup_into_configured_dir(tmp_path):
    video = tmp_path / "Movie (2010).mkv"
    video.write_bytes(b"y" * 100)
    backup_dir = tmp_path / "trash"

    backup = _backup_original(video, backup_dir, 14)

    assert backup is not None
    assert backup.parent == backup_dir
    assert backup.stat().st_ino == video.stat().st_ino


def test_prune_removes_old_backups(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    old = bdir / "old.bak"
    old.write_bytes(b"1")
    fresh = bdir / "fresh.bak"
    fresh.write_bytes(b"2")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    _prune_old_backups(bdir, retention_days=14)

    assert not old.exists()
    assert fresh.exists()


def test_prune_zero_retention_keeps_everything(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    old = bdir / "old.bak"
    old.write_bytes(b"1")
    stale = time.time() - 365 * 86400
    os.utime(old, (stale, stale))

    _prune_old_backups(bdir, retention_days=0)

    assert old.exists()
