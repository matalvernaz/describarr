"""Behaviour of the 2026-09-26 coverage round, end to end through real code.

- the two new rescues publish through ``_align_and_keep`` with a real report
  on disk (This Is Us S05E06's per-act map; Family Guy S21E01's low-score line);
- a donor byte-identical to one already tried is not aligned again;
- the positional fallback refuses a file titled as a different episode;
- a refusal's Pushover says a description was found;
- the Sonarr hook's episode title reaches ``process_episode``.
"""

import json
import zipfile

import describarr.server as srv
import describarr.workflow as workflow
from conftest import fake_config
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.matcher import extract_episode
from describarr.workflow import process_episode

from test_rate_profile import THIS_IS_US_S05E06


def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


# Family Guy S21E01 "Oscars Guy", DSNP: one native line over 99.5 % of 21 min,
# similarity 20.8 % — refused 2026-09-26 under the 30 % rescue floor.
FAMILY_GUY_S21E01 = [
    _seg(0.0, 0.7, 456.1, 0.0, 455.4),
    _seg(216991.8, 456.1, 458.2, 455.4, 455.4),
    _seg(0.0, 458.2, 1263.3, 455.4, 1260.4),
]


def _run(monkeypatch, tmp_path, segments, similarity, donor_name, episode_title,
         english=True):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Family.Guy.S21E01.Oscars.Guy.1080p.DSNP.WEB-DL.DD+5.1.H.264-NTb.mkv"
    video.write_bytes(b"x")
    audio = tmp_path / donor_name
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": 0.0, "segments": segments,
    }))
    published_calls = []
    monkeypatch.setattr(workflow, "align",
                        lambda *a, **k: AlignResult(combined, report, None, returncode=0))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published_calls.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    published, reason = workflow._align_and_keep(
        config, video, audio, label="Family Guy S21E01", episode_title=episode_title,
    )
    decisions = json.loads((config.cache_dir / "decisions.json").read_text())
    return published, published_calls, decisions[-1]


# ── the two rescues, through the real gate ───────────────────────────────────

def test_per_act_compression_publishes(monkeypatch, tmp_path):
    segs = [dict(s, audio_start_sec=0.0, audio_end_sec=0.0) for s in THIS_IS_US_S05E06]
    published, calls, decision = _run(
        monkeypatch, tmp_path, segs, 46.3, "5.06 Birth Mother.mp3", "Birth Mother",
    )
    assert published and len(calls) == 1
    assert decision["path"] == "mixed-rate-rescue"


def test_low_score_publishes_when_the_donor_names_this_episode(monkeypatch, tmp_path):
    published, calls, decision = _run(
        monkeypatch, tmp_path, FAMILY_GUY_S21E01, 20.8, "[S21.E01] Oscars Guy.mp3", "Oscars Guy",
    )
    assert published and len(calls) == 1
    assert decision["path"] == "corroborated-rescue"


def test_low_score_is_refused_when_the_donor_names_another_episode(monkeypatch, tmp_path):
    published, calls, decision = _run(
        monkeypatch, tmp_path, FAMILY_GUY_S21E01, 20.8, "[S21.E02] Bend or Blockbuster.mp3",
        "Oscars Guy",
    )
    assert not published and calls == []
    assert decision["outcome"] == "rejected"


def test_low_score_is_refused_for_a_near_identical_title(monkeypatch, tmp_path):
    # Review, 2026-09-26: a near spelling must not vouch for a 21 % donor.
    published, calls, _ = _run(
        monkeypatch, tmp_path, FAMILY_GUY_S21E01, 20.8, "[S01.E02] Pilots.mp3", "Pilot",
    )
    assert not published and calls == []


def test_low_score_is_refused_on_a_foreign_first_track(monkeypatch, tmp_path):
    published, calls, _ = _run(
        monkeypatch, tmp_path, FAMILY_GUY_S21E01, 20.4, "[S21.E01] Oscars Guy.mp3", "Oscars Guy",
        english=False,
    )
    assert not published and calls == []


def test_low_score_is_refused_with_no_title_evidence(monkeypatch, tmp_path):
    published, calls, _ = _run(
        monkeypatch, tmp_path, FAMILY_GUY_S21E01, 20.8, "Track 01.mp3", "Oscars Guy",
    )
    assert not published and calls == []


# ── a byte-identical donor is aligned once ───────────────────────────────────

class _Source:
    def __init__(self, files):
        self.files = files

    def episode_candidates(self, cache_dir, series_title, season, episode):
        return list(self.files)

    def close(self):
        pass


def _walk(monkeypatch, tmp_path, audiovault_bytes, mirror_bytes):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "This.Is.Us.S05E06.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb.mkv"
    video.write_bytes(b"v")
    av = tmp_path / "av" / "5.06 Birth Mother.mp3"
    av.parent.mkdir()
    av.write_bytes(audiovault_bytes)
    la = tmp_path / "la" / "5.06 Birth Mother.mp3"
    la.parent.mkdir()
    la.write_bytes(mirror_bytes)

    class Client:
        def search_shows(self, title):
            return [{"name": "This Is Us - Season 5 (2020)", "url": "https://av/5"}]

    aligned = []
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: tmp_path / "s5.zip")
    monkeypatch.setattr(workflow, "_episode_donor", lambda *a, **k: av)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [_Source([la])])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, **k:
                            (aligned.append(audio_path), (False, "similarity 46.3% — refused"))[1])
    process_episode(Client(), config, video, "This Is Us", 5, 6)
    return aligned, av, la


def test_an_identical_mirror_copy_is_not_aligned_again(monkeypatch, tmp_path):
    aligned, av, _ = _walk(monkeypatch, tmp_path, b"same master", b"same master")
    assert aligned == [av]


def test_a_different_recording_is_still_tried(monkeypatch, tmp_path):
    aligned, av, la = _walk(monkeypatch, tmp_path, b"one recording", b"another recording")
    assert aligned == [av, la]


# ── the positional fallback checks the title ─────────────────────────────────

def _season_two_zip(tmp_path, names):
    zip_path = tmp_path / "s2.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in names:
            zf.writestr(f"This Is Us - Season 2 (2017)/{name}", b"a")
    return zip_path


# AudioVault's This Is Us season 2 has no 2.06, so the sixth file is 2.07.
_SEASON_TWO = ["2.01 A Father's Advice.mp3", "2.02 A Manny-Splendored Thing.mp3", "2.03 Deja Vu.mp3",
               "2.04 Still There.mp3", "2.05 Brothers.mp3", "2.07 The Most Disappointed Man.mp3",
               "2.08 Number One.mp3"]


def test_positional_fallback_refuses_a_file_titled_as_another_episode(tmp_path):
    zip_path = _season_two_zip(tmp_path, _SEASON_TWO)
    assert extract_episode(zip_path, tmp_path / "x", 6, episode_title="The 20s") is None


def test_positional_fallback_still_works_without_a_title(tmp_path):
    zip_path = _season_two_zip(tmp_path, _SEASON_TWO)
    got = extract_episode(zip_path, tmp_path / "x", 6)
    assert got.name == "2.07 The Most Disappointed Man.mp3"


def test_positional_fallback_still_works_for_untitled_tracks(tmp_path):
    zip_path = _season_two_zip(tmp_path, [f"Track {i}.mp3" for i in range(1, 9)])
    got = extract_episode(zip_path, tmp_path / "x", 6, episode_title="The 20s")
    assert got.name == "Track 6.mp3"


# ── the Pushover says a description was found ────────────────────────────────

def test_a_refusal_says_a_description_was_found():
    msg = srv._notify_message(
        "no_match",
        "similarity 20.8% (coverage 99.9%, stable trunk 99.5%, median rate -0.00%, "
        "sync_ok=True) — no trusted sync signal",
    )
    assert msg.startswith("Found an audio description, but it did not line up with this copy")
    assert "match score 21%" in msg
    assert "no audio description available" not in msg.lower()


def test_an_engine_refusal_is_put_in_words():
    msg = srv._notify_message("no_match", "No obvious cause from energy analysis.")
    assert "the sound did not match" in msg


def test_nothing_found_says_so():
    assert srv._notify_message("no_match", None) == "No audio description found."


def test_no_message_claims_the_file_was_added():
    for outcome in ("described", "no_match", "queued", "error"):
        assert not srv._notify_message(outcome, None).startswith("Added")


# ── the hook's episode title reaches the walk ────────────────────────────────

def test_sonarr_hook_passes_the_episode_title(monkeypatch, tmp_path):
    video = tmp_path / "Family.Guy.S21E11.REPACK.1080p.WEB.H264-CAKES.mkv"
    video.write_bytes(b"x")
    seen = {}

    def fake_process_episode(client, config, video_path, title, season, episode, **kwargs):
        seen.update(kwargs)
        return True, None

    monkeypatch.setattr(srv, "process_episode", fake_process_episode)
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    srv._sonarr(fake_config(tmp_path), {
        "sonarr_eventtype": "Download",
        "sonarr_series_title": "Family Guy",
        "sonarr_series_year": "1999",
        "sonarr_episodefile_seasonnumber": "21",
        "sonarr_episodefile_episodenumbers": "11",
        "sonarr_episodefile_episodetitles": "Love Story Guy",
        "sonarr_episodefile_path": str(video),
    })
    assert seen["episode_title"] == "Love Story Guy"


# ── the title survives the daily-limit queue ─────────────────────────────────

def test_a_queued_episode_keeps_sonarrs_title(monkeypatch, tmp_path):
    from describarr.audiovault import DailyLimitReached
    from describarr.retry_queue import RetryQueue
    video = tmp_path / "Family.Guy.S21E11.REPACK.1080p.WEB.H264-CAKES.mkv"
    video.write_bytes(b"x")
    queue_path = tmp_path / "retry_queue.json"
    queue = RetryQueue(queue_path)

    def over_limit(*a, **k):
        raise DailyLimitReached("cap")

    monkeypatch.setattr(srv, "process_episode", over_limit)
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_get_retry_queue", lambda config: queue)
    srv._sonarr(fake_config(tmp_path), {
        "sonarr_series_title": "Family Guy",
        "sonarr_episodefile_seasonnumber": "21",
        "sonarr_episodefile_episodenumbers": "11",
        "sonarr_episodefile_episodetitles": "Love Story Guy",
        "sonarr_episodefile_path": str(video),
    })
    queued = json.loads(queue_path.read_text())
    entries = queued if isinstance(queued, list) else queued.get("items", [])
    assert entries[-1]["episode_title"] == "Love Story Guy"


def test_the_drain_hands_the_queued_title_on(monkeypatch, tmp_path):
    from describarr.retry_queue import RetryQueue
    video = tmp_path / "Family.Guy.S21E11.REPACK.1080p.WEB.H264-CAKES.mkv"
    video.write_bytes(b"x")
    queue = RetryQueue(tmp_path / "retry_queue.json")
    queue.add_episodes("Family Guy", 21, [11], str(video), episode_title="Love Story Guy")
    seen = {}

    def fake_process_episode(client, config, video_path, title, season, episode, **kwargs):
        seen.update(kwargs)
        return True, None

    monkeypatch.setattr(workflow, "process_episode", fake_process_episode)
    workflow.drain_retry_queue(queue, object(), Config(email="e", password="p", cache_dir=tmp_path / "c"))
    assert seen.get("episode_title") == "Love Story Guy"
