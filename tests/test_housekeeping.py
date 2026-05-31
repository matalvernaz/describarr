"""Cleanup sweeps, the season.episode matcher pattern, and the AD-track probe."""

import json
import os
import time

import describarr.workflow as wf
from describarr.matcher import extract_episode
from describarr.aligner import source_has_ad_track


def test_prune_output_scratch_removes_only_orphans(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    old_run = out / "run-old"
    old_run.mkdir()
    (old_run / "ad_x.mkv").write_bytes(b"1")
    new_run = out / "run-new"
    new_run.mkdir()
    old_stray = out / "ad_Legacy.mkv"
    old_stray.write_bytes(b"1")
    fresh_stray = out / "ad_Fresh.mkv"
    fresh_stray.write_bytes(b"1")

    ancient = time.time() - 3 * 3600  # older than the 2h orphan floor
    os.utime(old_run, (ancient, ancient))
    os.utime(old_stray, (ancient, ancient))

    wf.prune_output_scratch(tmp_path)

    assert not old_run.exists()      # orphaned run dir gone
    assert not old_stray.exists()    # legacy stray output gone
    assert new_run.exists()          # recent run dir (could be live) kept
    assert fresh_stray.exists()      # recent stray kept


def test_prune_registered_backups(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    bdir = tmp_path / "Season 1" / ".describarr_backup"
    bdir.mkdir(parents=True)
    old_ts = int(time.time() - 30 * 86400)
    new_ts = int(time.time())
    stale = bdir / f"a.mkv.{old_ts}.bak"
    stale.write_bytes(b"1")
    fresh = bdir / f"b.mkv.{new_ts}.bak"
    fresh.write_bytes(b"2")
    gone = tmp_path / "deleted" / ".describarr_backup"  # never created on disk

    reg = cache / "backup_dirs.json"
    reg.write_text(json.dumps([str(bdir), str(gone)]))

    wf.prune_registered_backups(cache, retention_days=14)

    assert not stale.exists()
    assert fresh.exists()
    dirs = json.loads(reg.read_text())
    assert str(bdir) in dirs       # still holds a backup → kept
    assert str(gone) not in dirs   # dead entry dropped


def test_extract_episode_matches_season_dot_episode(tmp_path):
    # AudioVault "4.09 Title.mp3" disc format — the pattern this commit adds.
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    (extract_dir / ".extracted").touch()
    for n in ("4.08 Previous.mp3", "4.09 And the Past.mp3", "4.10 Next.mp3"):
        (extract_dir / n).write_bytes(b"x")
    zip_path = tmp_path / "season.zip"
    zip_path.write_bytes(b"PK")  # non-audio suffix; extraction skipped via marker

    got = extract_episode(zip_path, extract_dir, 9)

    assert got is not None
    assert got.name == "4.09 And the Past.mp3"


def test_source_has_ad_track_false_on_unprobeable(tmp_path):
    # A probe failure must be conservative (False) so it never blocks a real
    # first-time alignment.
    assert source_has_ad_track(tmp_path / "does-not-exist.mkv") is False
