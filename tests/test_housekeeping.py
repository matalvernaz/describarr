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


def _seed_season_zip(tmp_path, total):
    """Build a show cache dir with a season zip + full extract dir of *total*
    audio files, plus a manifest mapping a URL to that zip. Returns
    (zip_cache_dir, season_dir, zip_path)."""
    zip_cache_dir = tmp_path / "shows" / "show"
    season_dir = zip_cache_dir / "season_03"
    extract_dir = season_dir / "show_-_season_3"
    extract_dir.mkdir(parents=True)
    (extract_dir / ".extracted").touch()
    for n in range(1, total + 1):
        (extract_dir / f"E{n:02d}.mp3").write_bytes(b"x")
    zip_path = zip_cache_dir / "show - Season 3.zip"
    zip_path.write_bytes(b"PK")
    (zip_cache_dir / "manifest.json").write_text(
        json.dumps({"https://example/dl/1": str(zip_path)})
    )
    return zip_cache_dir, season_dir, zip_path, extract_dir


def test_mark_done_cleans_up_only_on_completion_transition(tmp_path):
    # A season that completes exactly once should have its zip cleaned once.
    zip_cache_dir, season_dir, zip_path, extract_dir = _seed_season_zip(tmp_path, 2)

    wf._mark_episode_done(zip_cache_dir, 3, 1, extract_dir, zip_path)
    assert zip_path.exists()  # 1/2 done — keep the zip for the sibling episode

    wf._mark_episode_done(zip_cache_dir, 3, 2, extract_dir, zip_path)
    assert not zip_path.exists()  # 2/2 — transition into complete → cleaned


def test_mark_done_on_saturated_season_keeps_zip(tmp_path):
    # Reprocessing an already-complete season (the download-cap-burning bug):
    # the zip must survive so the season's other queued episodes hit cache
    # instead of forcing a fresh AudioVault download per episode.
    zip_cache_dir, season_dir, zip_path, extract_dir = _seed_season_zip(tmp_path, 2)
    progress = zip_cache_dir / ".done_s03.json"
    progress.write_text(json.dumps({"total": 2, "done": [1, 2]}))

    wf._mark_episode_done(zip_cache_dir, 3, 1, extract_dir, zip_path)

    assert zip_path.exists()  # already complete → no re-clean, zip preserved
    manifest = json.loads((zip_cache_dir / "manifest.json").read_text())
    assert "https://example/dl/1" in manifest  # manifest entry preserved → cache hit


def test_source_has_ad_track_false_on_unprobeable(tmp_path):
    # A probe failure must be conservative (False) so it never blocks a real
    # first-time alignment.
    assert source_has_ad_track(tmp_path / "does-not-exist.mkv") is False
