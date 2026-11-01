"""describarr's log and decision history outlive the container.

On 2026-10-13 a storage move recreated the container, and with its log went the
only account of three days' work: why five SVU episodes with clean, drift-free
maps were refused could no longer be answered, because no file kept which
recording had been tried on which episode, or what the gate had read. These
pin the two files that now keep it (see ``describarr.log_files``) and what a
decision records.
"""

import json
import logging
import os
import zipfile
from datetime import date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

import describarr.aligner as aligner
import describarr.log_files as log_files
import describarr.server as server
import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.decision_log import DecisionLog
from describarr.log_files import (
    LogSettings,
    append_history,
    attach_file_log,
    history_dir,
    log_settings,
    prune_decision_history,
    retention_days,
)
from describarr.workflow import process_episode, process_movie


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch):
    """Each test starts with the default retention, no warning remembered and
    no file handler left behind by another test."""
    monkeypatch.delenv("DESCRIBARR_LOG_RETENTION_DAYS", raising=False)
    log_files._warned.clear()
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    log_files._warned.clear()


def _history_lines(directory: Path) -> list[dict]:
    lines = []
    for path in sorted(directory.glob("decisions-*.jsonl")):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return lines


# ── the retention setting never stops the server ─────────────────────────────

def test_retention_is_a_year_unless_set(monkeypatch):
    assert retention_days() == 365
    monkeypatch.setenv("DESCRIBARR_LOG_RETENTION_DAYS", "  ")
    assert retention_days() == 365
    monkeypatch.setenv("DESCRIBARR_LOG_RETENTION_DAYS", "30")
    assert retention_days() == 30


def test_an_unreadable_retention_is_the_default_and_said_once(monkeypatch, caplog):
    monkeypatch.setenv("DESCRIBARR_LOG_RETENTION_DAYS", "a year")
    with caplog.at_level(logging.WARNING, logger="describarr.log_files"):
        assert retention_days() == 365
        assert retention_days() == 365
    warnings = [r for r in caplog.records if "DESCRIBARR_LOG_RETENTION_DAYS" in r.getMessage()]
    assert len(warnings) == 1


def test_a_negative_retention_keeps_nothing(monkeypatch):
    monkeypatch.setenv("DESCRIBARR_LOG_RETENTION_DAYS", "-3")
    assert retention_days() == 0
    assert history_dir(Path("/cache")) is None


@pytest.mark.parametrize("cache_env", ["{tmp}/state", "~/state", None])
def test_the_log_lives_in_the_same_cache_as_everything_else(monkeypatch, tmp_path, cache_env):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUDIOVAULT_EMAIL", "e")
    monkeypatch.setenv("AUDIOVAULT_PASSWORD", "p")
    if cache_env is None:
        monkeypatch.delenv("DESCRIBARR_CACHE_DIR", raising=False)
    else:
        monkeypatch.setenv("DESCRIBARR_CACHE_DIR", cache_env.format(tmp=tmp_path))
    cache = Config.from_env().cache_dir
    assert "~" not in str(cache)
    assert log_settings().log_dir == cache / "logs"
    assert history_dir(cache) == cache / "logs"


# ── the log file ──────────────────────────────────────────────────────────────

def test_everything_logged_is_copied_to_a_daily_file(tmp_path):
    handler = attach_file_log(LogSettings(tmp_path / "logs", 30))
    assert isinstance(handler, TimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT" and handler.backupCount == 30
    assert handler.encoding == "utf-8"
    logging.getLogger("describarr.workflow").warning("Looking up: Pokémon S01E01")
    handler.flush()
    text = (tmp_path / "logs" / "describarr.log").read_text(encoding="utf-8")
    assert "describarr.workflow: Looking up: Pokémon S01E01" in text


def test_attaching_twice_keeps_one_handler(tmp_path):
    settings = LogSettings(tmp_path / "logs", 30)
    first = attach_file_log(settings)
    assert attach_file_log(settings) is first
    files = [h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)]
    assert files == [first]


def test_no_log_file_when_retention_is_zero(tmp_path):
    assert attach_file_log(LogSettings(tmp_path / "logs", 0)) is None
    assert not (tmp_path / "logs").exists()


def test_a_log_directory_that_cannot_be_made_costs_one_warning(tmp_path, caplog):
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the cache should be")
    with caplog.at_level(logging.WARNING, logger="describarr.log_files"):
        assert attach_file_log(LogSettings(blocker / "logs", 30)) is None
    assert not any(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers)
    assert len([r for r in caplog.records if "Cannot keep a log file" in r.getMessage()]) == 1


class _Stop(Exception):
    """Raised in place of the HTTP server, so serve() returns before serving."""


def _serve_until_the_server(monkeypatch):
    def no_server(*a, **k):
        raise _Stop
    monkeypatch.setattr(server, "_ThreadingHTTPServer", no_server)
    with pytest.raises(_Stop):
        server.serve(0)


def test_serve_keeps_the_log_file_before_it_does_anything(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)               # the server's own level (see __main__)
    monkeypatch.setenv("DESCRIBARR_CACHE_DIR", str(tmp_path / "cache"))
    _serve_until_the_server(monkeypatch)
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)]
    assert [h.baseFilename for h in handlers] == [str(tmp_path / "cache" / "logs" / "describarr.log")]
    handlers[0].flush()
    assert "describarr starting with describealaign" in \
        (tmp_path / "cache" / "logs" / "describarr.log").read_text(encoding="utf-8")


def test_serve_carries_on_when_the_log_file_cannot_be_kept(monkeypatch, tmp_path):
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DESCRIBARR_CACHE_DIR", str(blocker))
    _serve_until_the_server(monkeypatch)       # reaches the server: nothing raised before it


# ── the decision history ─────────────────────────────────────────────────────

def test_the_history_keeps_what_a_ring_of_none_keeps_nothing_of(tmp_path):
    ring = tmp_path / "decisions.json"
    DecisionLog(ring, 0, history_dir=tmp_path / "logs").append({"title": "Show S01E01"})
    assert not ring.exists()
    assert [line["title"] for line in _history_lines(tmp_path / "logs")] == ["Show S01E01"]


def test_the_history_keeps_every_decision_the_ring_drops(tmp_path):
    log = DecisionLog(tmp_path / "decisions.json", 2, history_dir=tmp_path / "logs")
    for n in range(5):
        log.append({"title": f"Show S01E0{n}", "outcome": "rejected"})
    assert len(log.load()) == 2
    lines = _history_lines(tmp_path / "logs")
    assert [line["title"] for line in lines] == [f"Show S01E0{n}" for n in range(5)]
    assert all(line["ts"] for line in lines)
    month = datetime.now().strftime("%Y-%m")
    assert [p.name for p in (tmp_path / "logs").iterdir()] == [f"decisions-{month}.jsonl"]


def test_a_ring_that_cannot_be_written_still_costs_the_history_nothing(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the ring's folder should be")
    log = DecisionLog(blocker / "decisions.json", 50, history_dir=tmp_path / "logs")
    with pytest.raises(OSError):                 # the ring's failure still reaches the caller
        log.append({"title": "Show S01E01"})
    assert [line["title"] for line in _history_lines(tmp_path / "logs")] == ["Show S01E01"]


def test_a_history_that_cannot_be_written_still_costs_the_ring_nothing(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the history's folder should be")
    log = DecisionLog(tmp_path / "decisions.json", 50, history_dir=blocker / "logs")
    with caplog.at_level(logging.WARNING, logger="describarr.log_files"):
        log.append({"title": "Show S01E01"})
    assert [e["title"] for e in log.load()] == ["Show S01E01"]
    assert any("Could not add a decision" in r.getMessage() for r in caplog.records)


def test_a_history_line_keeps_any_value_as_text(tmp_path):
    append_history(tmp_path, {"video": Path("/tv/Show/S01E01.mkv"), "title": "Amélie"},
                   now=datetime(2026, 10, 15, 12, 0))
    line = (tmp_path / "decisions-2026-10.jsonl").read_text(encoding="utf-8")
    assert line.endswith("\n") and line.count("\n") == 1
    assert json.loads(line) == {"video": "/tv/Show/S01E01.mkv", "title": "Amélie"}


# ── old months go, nothing else does ─────────────────────────────────────────

def test_only_months_past_the_retention_are_pruned(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    names = ["decisions-2025-01.jsonl", "decisions-2026-09.jsonl", "decisions-2026-10.jsonl",
             "decisions-2025-13.jsonl", "decisions-2025-01.jsonl.bak", "notes.txt",
             "describarr.log.2025-01-01"]
    for name in names:
        (logs / name).write_text("x")
    (logs / "decisions-2024-12.jsonl").mkdir()   # a folder by that name is not ours to delete
    removed = prune_decision_history(LogSettings(logs, 15), today=date(2026, 10, 15))
    assert removed == 1
    assert sorted(p.name for p in logs.iterdir()) == sorted(
        n for n in names + ["decisions-2024-12.jsonl"] if n != "decisions-2025-01.jsonl")


def test_a_month_counts_from_its_last_day(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "decisions-2026-09.jsonl").write_text("x")
    settings = LogSettings(logs, 15)
    assert prune_decision_history(settings, today=date(2026, 10, 15)) == 0   # 15 days after 09-30
    assert prune_decision_history(settings, today=date(2026, 10, 16)) == 1


def test_nothing_is_pruned_when_logs_are_off(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "decisions-2020-01.jsonl").write_text("x")
    assert prune_decision_history(LogSettings(logs, 0), today=date(2026, 10, 15)) == 0
    assert (logs / "decisions-2020-01.jsonl").exists()


def test_the_nightly_prune_runs_without_a_configuration_and_survives_failing(monkeypatch):
    events = []

    def prune():
        events.append("prune")
        raise OSError("the share went away")

    def config():
        events.append("config")
        raise ValueError("AUDIOVAULT_EMAIL and AUDIOVAULT_PASSWORD must be set.")

    sleeps = iter([None, _Stop()])

    def sleep(seconds):
        nxt = next(sleeps)
        if nxt is not None:
            raise nxt

    monkeypatch.setattr(server, "prune_decision_history", prune)
    monkeypatch.setattr(server.Config, "from_env", staticmethod(config))
    monkeypatch.setattr(server.time, "sleep", sleep)
    with pytest.raises(_Stop):
        server._midnight_drain_loop()
    assert events == ["prune", "config"]


# ── what a decision records ──────────────────────────────────────────────────

def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


# One straight native-rate line over a 25-minute episode: perfect timing.
_STRAIGHT = [_seg(0.0, 0.0, 1500.0, 0.0, 1500.0)]


def _decide(monkeypatch, tmp_path, similarity, *, donor_name="[S01.E01] Pilot.mp3",
            title="Pilot", english=True, result=None, video_exists=True, in_cache=True):
    """Run the real ``_align_and_keep`` over a report on disk; the last
    decision in the ring and every line of the history."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "tv" / "Show" / "Show.S01E01.mkv"
    video.parent.mkdir(parents=True)
    if video_exists:
        video.write_bytes(b"x")
    folder = config.cache_dir / "shows" / "Show" / "season_01" if in_cache else tmp_path / "loose"
    folder.mkdir(parents=True)
    audio = folder / donor_name
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir(parents=True)
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True)
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": 0.0, "segments": _STRAIGHT,
    }))
    if result is None:
        result = AlignResult(combined, report, None, returncode=0, aligned_english=english)
    monkeypatch.setattr(workflow, "align", lambda *a, **k: result)
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 1500.0)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    published, _ = workflow._align_and_keep(
        config, video, audio, label="Show S01E01", episode_title=title,
        series_title="Show", source="AudioVault",
    )
    ring = json.loads((config.cache_dir / "decisions.json").read_text())
    return published, ring[-1], _history_lines(config.cache_dir / "logs"), video


def test_a_publish_records_the_recording_its_source_and_what_the_gate_read(monkeypatch, tmp_path):
    published, decision, history, video = _decide(monkeypatch, tmp_path, 75.0)
    assert published and decision["outcome"] == "described"
    assert decision["video"] == str(video)
    assert decision["donor"] == "shows/Show/season_01/[S01.E01] Pilot.mp3"
    assert decision["source"] == "AudioVault"
    assert decision["episode_title"] == "Pilot" and decision["min_score"] == 60.0
    assert decision["path"] == "similarity" and decision["sync_ok"] is True
    assert decision["piecewise"] == pytest.approx(100.0)
    assert decision["uncovered"] == pytest.approx(0.0) and decision["longest_gap"] == 0.0
    assert decision["described_runtime"] == pytest.approx(1500.0)
    # Above the rescue's band nothing was asked of the title or the track.
    assert decision["title_corroborated"] is None and decision["english_track"] is None
    assert "engine" in decision
    assert history == [decision]


def test_a_corroborated_rescue_records_the_title_and_the_track(monkeypatch, tmp_path):
    published, decision, history, _ = _decide(monkeypatch, tmp_path, 25.0)
    assert published and decision["path"] == "corroborated-rescue"
    assert decision["title_corroborated"] is True and decision["english_track"] is True
    assert history[-1] == decision


def test_a_refusal_records_the_title_that_did_not_agree_and_no_track_reading(monkeypatch, tmp_path):
    # The SVU shape: a perfect line, a score under the rescue floor. The record
    # must say the gate read the title as another episode's, and that it never
    # looked at the track's language (that is only asked once the title agrees).
    published, decision, history, _ = _decide(
        monkeypatch, tmp_path, 25.0, donor_name="[S01.E02] Second.mp3")
    assert not published and decision["outcome"] == "rejected"
    assert decision["title_corroborated"] is False and decision["english_track"] is None
    assert decision["donor"] == "shows/Show/season_01/[S01.E02] Second.mp3"
    assert decision["score"] == pytest.approx(25.0)
    assert history[-1] == decision


def test_a_non_english_track_is_recorded_as_read(monkeypatch, tmp_path):
    published, decision, _, _ = _decide(monkeypatch, tmp_path, 25.0, english=False)
    assert not published
    assert decision["title_corroborated"] is True and decision["english_track"] is False


def test_a_recording_outside_the_cache_is_named_in_full(monkeypatch, tmp_path):
    _, decision, _, _ = _decide(monkeypatch, tmp_path, 75.0, in_cache=False)
    assert decision["donor"] == str(tmp_path / "loose" / "[S01.E01] Pilot.mp3")


def test_an_engine_refusal_keeps_the_engines_own_figures(monkeypatch, tmp_path):
    figures = {"error": "alignment_mismatch", "duration_ratio": 0.946}
    result = AlignResult(None, None, "No obvious cause from energy analysis.", returncode=1,
                         diagnostic=figures)
    published, decision, history, _ = _decide(monkeypatch, tmp_path, 0.0, result=result)
    assert not published and decision["outcome"] == "failed"
    assert decision["detail"] == "No obvious cause from energy analysis."
    assert decision["diagnostic"] == figures
    assert decision["donor"] == "shows/Show/season_01/[S01.E01] Pilot.mp3"
    assert history[-1] == decision


def test_a_video_gone_before_the_alignment_is_recorded(monkeypatch, tmp_path):
    published, decision, history, video = _decide(monkeypatch, tmp_path, 75.0, video_exists=False)
    assert not published
    assert decision["outcome"] == "error"
    assert decision["detail"] == "source file vanished before alignment"
    assert decision["video"] == str(video) and decision["source"] == "AudioVault"
    assert history[-1] == decision


# ── every caller says where the recording came from ──────────────────────────

class _Elsewhere:
    """An extra source with a label, offering one recording for anything."""

    label = "Elsewhere"

    def __init__(self, recording):
        self.recording = recording

    def episode_candidates(self, cache_dir, series_title, season, episode, episode_title=""):
        return [self.recording]

    def movie_candidates(self, cache_dir, title, year):
        return [self.recording]

    def close(self):
        pass


def _refuse_and_note(tried):
    def gate(config, video_path, audio_path, label=None, source="", **_):
        tried.append((audio_path.name, source))
        return False, "similarity 2.3% — refused"
    return gate


def test_an_episodes_candidates_carry_their_sources(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "tv" / "Show.S01E02.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in ("01 Alpha.mp3", "02 Beta.mp3", "03 Gamma.mp3"):
            zf.writestr(f"Show - Season 1/{name}", name.encode())
    elsewhere = tmp_path / "elsewhere" / "1.02 Beta.mp3"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"another recording")

    class Client:
        def search_shows(self, title):
            return [{"name": "Show - Season 1", "url": "https://av/pack"}]

    tried = []
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_align_and_keep", _refuse_and_note(tried))
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [_Elsewhere(elsewhere)])
    described, _ = process_episode(Client(), config, video, "Show", 1, 2, episode_title="Beta")
    assert not described
    assert tried[0] == ("02 Beta.mp3", "AudioVault")             # the pack's own pick
    assert ("1.02 Beta.mp3", "Elsewhere") in tried                # the extra source
    neighbours = [t for t in tried if t[0] in ("01 Alpha.mp3", "03 Gamma.mp3")]
    assert neighbours and all(source == "AudioVault" for _, source in neighbours)


def test_a_films_candidates_carry_their_sources(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    vault = tmp_path / "vault.mp3"
    vault.write_bytes(b"one recording")
    elsewhere = tmp_path / "elsewhere.mp3"
    elsewhere.write_bytes(b"another recording")

    class Client:
        def search_movies(self, title):
            return [{"name": "Movie (2020)", "url": "https://av/dl/1"}]

    tried = []
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_movie", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: vault)
    monkeypatch.setattr(workflow, "_align_and_keep", _refuse_and_note(tried))
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [_Elsewhere(elsewhere)])
    described, _ = process_movie(Client(), config, video, "Movie", "2020")
    assert not described
    assert tried == [("vault.mp3", "AudioVault"), ("elsewhere.mp3", "Elsewhere")]


# ── the server's own records, joins, and the engine's figures ────────────────

def test_a_decision_with_no_alignment_names_its_file(tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    server._log_terminal_decision(config, "Show S01E01", "no_match", None,
                                  path="/tv/Show/Show.S01E01.mkv")
    ring = json.loads((config.cache_dir / "decisions.json").read_text())
    assert ring[-1]["video"] == "/tv/Show/Show.S01E01.mkv"
    assert _history_lines(config.cache_dir / "logs") == ring


def test_a_reused_join_names_its_parts(monkeypatch, tmp_path, caplog):
    parts = [tmp_path / "02.12 One.mp3", tmp_path / "02.13 Two.mp3"]
    for part in parts:
        part.write_bytes(b"a")
    out = tmp_path / "joined" / "s02E12E13.mp3"
    out.parent.mkdir()
    out.write_bytes(b"ab")
    monkeypatch.setattr(workflow, "_audio_duration",
                        lambda p: 2400.0 if p == out else 1200.0)
    with caplog.at_level(logging.INFO, logger="describarr.workflow"):
        assert workflow._concat_audio(parts, out) == out
    message = " ".join(r.getMessage() for r in caplog.records)
    assert str(parts[0]) in message and str(parts[1]) in message


def _sidecar(directory, name, content, mtime=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(content))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_the_engines_figures_are_kept_short(tmp_path):
    video = tmp_path / "Show.S05E08.mkv"
    _sidecar(tmp_path / "align", "Show.S05E08.fail.json", {
        "version": "2.2.7", "error": "alignment_mismatch",
        "summary": "No obvious cause from energy analysis.",
        "diagnostic": {
            "summary": "No obvious cause from energy analysis.",
            "duration_ratio": 0.946, "video_duration_minutes": 43.28,
            "audio_quiet_fraction": 0.001, "clipped": True, "missing": None,
            "note": "x" * 5000, "spans": [[0, 1]] * 1000, "nested": {"a": 1},
        },
    })
    figures = aligner._failure_diagnostic(video, tmp_path / "align")
    assert figures == {
        "error": "alignment_mismatch", "duration_ratio": 0.946,
        "video_duration_minutes": 43.28, "audio_quiet_fraction": 0.001,
        "clipped": True, "missing": None, "note": "x" * 200,
    }
    assert aligner._read_failure_sidecar(video, tmp_path / "align") == \
        "No obvious cause from energy analysis."


def test_an_earlier_runs_figures_are_not_this_runs(tmp_path):
    video = tmp_path / "Show.S05E08.mkv"
    _sidecar(tmp_path / "align", "Show.S05E08.fail.json",
             {"error": "alignment_mismatch", "diagnostic": {"duration_ratio": 0.9}}, mtime=1000.0)
    assert aligner._failure_diagnostic(video, tmp_path / "align", min_mtime=2000.0) is None


def test_an_engine_refusal_hands_back_its_figures(monkeypatch, tmp_path):
    video = tmp_path / "Show.S05E08.mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")

    def engine(cmd, timeout=None):
        _sidecar(tmp_path / "align", "Show.S05E08.fail.json", {
            "error": "alignment_mismatch",
            "summary": "No obvious cause from energy analysis.",
            "diagnostic": {"duration_ratio": 0.946},
        })
        return 1, "", "engine noise"

    monkeypatch.setattr(aligner, "_run_subprocess", engine)
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)
    res = aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)
    assert res.failure_reason == "No obvious cause from energy analysis."
    assert res.diagnostic == {"error": "alignment_mismatch", "duration_ratio": 0.946}


def test_the_engine_version_is_read_once_and_may_be_unknown(monkeypatch):
    calls = []

    def version(name):
        calls.append(name)
        return "2.2.7"

    aligner.engine_version.cache_clear()
    monkeypatch.setattr(aligner.importlib.metadata, "version", version)
    try:
        assert aligner.engine_version() == "2.2.7"
        assert aligner.engine_version() == "2.2.7"
        assert calls == ["describealaign"]
        aligner.engine_version.cache_clear()

        def missing(name):
            raise aligner.importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(aligner.importlib.metadata, "version", missing)
        assert aligner.engine_version() is None
    finally:
        aligner.engine_version.cache_clear()
