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


def test_backup_of_old_file_survives_its_own_prune(tmp_path):
    # Regression: a backup is a hardlink, so it inherits the original file's
    # (old) mtime. Pruning by mtime would delete the backup the instant it was
    # created. The just-made backup of a 90-day-old file must survive.
    video = tmp_path / "Old.Show.S01E01.mkv"
    video.write_bytes(b"z" * 100)
    ancient = time.time() - 90 * 86400
    os.utime(video, (ancient, ancient))

    backup = _backup_original(video, None, 14)  # prune runs inside

    assert backup is not None
    assert backup.exists()


def test_prune_uses_filename_timestamp_not_mtime(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    old_ts = int(time.time() - 30 * 86400)
    new_ts = int(time.time())
    old = bdir / f"old.mkv.{old_ts}.bak"
    old.write_bytes(b"1")
    fresh = bdir / f"fresh.mkv.{new_ts}.bak"
    fresh.write_bytes(b"2")
    # Give both a fresh mtime to prove the decision is by filename, not mtime.
    now = time.time()
    os.utime(old, (now, now))
    os.utime(fresh, (now, now))

    _prune_old_backups(bdir, retention_days=14)

    assert not old.exists()
    assert fresh.exists()


def test_prune_leaves_unparseable_names(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    weird = bdir / "no-timestamp.bak"
    weird.write_bytes(b"1")

    _prune_old_backups(bdir, retention_days=14)

    assert weird.exists()


def test_prune_zero_retention_keeps_everything(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    old = bdir / f"old.mkv.{int(time.time() - 365 * 86400)}.bak"
    old.write_bytes(b"1")

    _prune_old_backups(bdir, retention_days=0)

    assert old.exists()
