"""
Fuzzy matching helpers.

Matches search results from AudioVault against show/movie titles and
locates the correct episode MP3 inside an extracted season zip.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Audio file extensions that describealaign accepts.
_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".wav", ".aac", ".flac", ".ac3", ".mka"}

# Natural-sort key: chunk a string into alternating text/digit runs so that
# "track2" sorts before "track10". Lifted onto Path objects via str(p).
_NATSORT_CHUNK_RE = re.compile(r"(\d+)")


def _natsort_key(name: str) -> list:
    return [int(part) if part.isdigit() else part.lower()
            for part in _NATSORT_CHUNK_RE.split(name)]


def _natsort_paths(paths) -> list[Path]:
    return sorted(paths, key=lambda p: _natsort_key(str(p)))


# ------------------------------------------------------------------
# Title / season matching
# ------------------------------------------------------------------

# AudioVault wraps catalog metadata in square brackets after the title:
# description variant ([New Description], [Old Description], [TTS]), narration
# region ([US], [UK]), or narration language ([Persian Description], [French
# Description], …). Square brackets never appear in real titles, so every
# bracketed tag is stripped before title similarity is scored — a region tag
# must not token-match a title (the movie "Us" otherwise Jaccard-matches the
# entire [US] catalog and the candidate walk burns the daily download cap).
# Tags are consulted only afterwards: quality to break ties between genuine
# variants, language to reject unusable narrations.
_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*")

# A "<language> Description" tag marks narration in that language. The program
# audio underneath is still the right film, so alignment can PASS on a
# non-English narration and publish it as the default track — reject the
# candidate outright instead. "New"/"Old" are variant labels, not languages.
_DESCRIPTION_LANG_RE = re.compile(r"\[\s*(\w+)\s+description\s*\]", re.IGNORECASE)
_ALLOWED_DESCRIPTION_LANGS = frozenset({"new", "old", "english"})


def _foreign_narration(name: str) -> bool:
    """True when *name* carries a non-English narration-language tag."""
    m = _DESCRIPTION_LANG_RE.search(name)
    return bool(m) and m.group(1).lower() not in _ALLOWED_DESCRIPTION_LANGS


def _variant_quality(name: str) -> int:
    """Rank a candidate's description variant; higher is preferred.

    [New Description]/untagged human AD (2) > [Old Description] (1) > [TTS] (0).
    Used only as a tiebreaker between variants whose stripped titles match
    equally well, so the best obtainable description is attempted before TTS
    instead of by accident of tag-string length.
    """
    n = name.lower()
    if "[tts]" in n:
        return 0
    if "[old description]" in n:
        return 1
    return 2

def find_season(
    results: list[dict], title: str, season: int, series_year: str = "",
) -> list[dict]:
    """
    Return all results from *results* that plausibly match *title* and *season*,
    ranked by title similarity (best first).

    Pass 1 returns candidates that explicitly name the season (e.g. "Season 2").
    Pass 2 (season 1 only) appends year-only entries (e.g. "Ted (2024)") as
    lower-priority fallbacks, for shows AudioVault hasn't split into seasons yet.

    *series_year* is the year the SERIES began, from Sonarr. A season entry's
    parenthesised year is that season's air year, not the series', so it cannot
    be compared for equality the way ``find_movie`` compares a film's — a
    correct "Gossip Girl - Season 5 (2011)" belongs to a 2007 series. It is
    used two softer ways instead, both of which leave a revival like a 2023
    season 11 of a 1999 series alone:

      * a candidate dated before the series began is dropped, which is sound
        for any season; and
      * candidates are ranked by how near their year sits to the season's
        expected air year, then the walk is confined to the winner's year
        (see ``_lock_to_release_year``).

    The caller should try each candidate in order, stopping on the first that
    aligns above the score threshold.
    """
    # Word-boundary regexes — plain substring containment incorrectly matched
    # ``"season 1"`` inside ``"Season 10"`` (and ``s1`` inside ``s10``), which
    # could route a Season-1 grab to a Season-10 candidate. The ``0?`` makes a
    # single regex match both ``Season 1`` and ``Season 01`` forms.
    season_patterns = [
        re.compile(rf"\bseason\s*0?{season}\b", re.IGNORECASE),
        re.compile(rf"\bseries\s*0?{season}\b", re.IGNORECASE),
        re.compile(rf"\bs0?{season}\b", re.IGNORECASE),
    ]
    # Catches *any* season marker — used to exclude clearly-numbered seasons
    # from the season-1 year-only fallback pool, regardless of zero-padding.
    any_season_marker = re.compile(r"\b(?:s|season|series)\s*0?\d+\b", re.IGNORECASE)

    results = [r for r in results if not _foreign_narration(r["name"])]
    title_lower = title.lower()

    start_year = int(series_year) if series_year.strip().isdigit() else None
    if start_year is not None:
        results = [
            r for r in results
            if (_release_year(r["name"]) or start_year) >= start_year
        ]
    # One season per year is the ceiling, so season N airs no earlier than
    # this. Later is ordinary (hiatus, revival) and carries no penalty beyond
    # the distance itself.
    expected_year = None if start_year is None else start_year + season - 1

    def _year_distance(name: str) -> int:
        """Sort key: how far a candidate's year sits from the expected one.
        Yearless candidates sort as an exact match so they are never demoted
        below a wrong-year one."""
        if expected_year is None:
            return 0
        year = _release_year(name)
        return 0 if year is None else abs(year - expected_year)

    def _ranked_above(candidates: list[dict], threshold: float) -> list[dict]:
        # Strip bracketed tags before scoring so all variants of a season tie
        # on title similarity; quality then breaks the tie (human AD before
        # TTS). Title match stays the dominant key, so a wrong show/season can
        # never be promoted over a near-exact match by quality alone. The
        # similarity is rounded so a sub-0.01 wobble between otherwise-identical
        # titles can't defeat the quality tiebreaker.
        scored = [
            (_title_similarity(title_lower, _BRACKET_TAG_RE.sub(" ", r["name"]).strip().lower()),
             _variant_quality(r["name"]), r)
            for r in candidates
        ]
        # Year proximity outranks variant quality but never title similarity:
        # a near-miss title must not be promoted for having a tidy year, while
        # the human-before-TTS tiebreak still decides between variants of the
        # same season.
        scored.sort(
            key=lambda x: (round(x[0], 2), -_year_distance(x[2]["name"]), x[1]),
            reverse=True,
        )
        kept = [(s, q, r) for s, q, r in scored if s >= threshold]
        for s, q, r in kept:
            logger.info("Season candidate: %r (score %.2f)", r["name"], s)
        if scored and not kept:
            logger.warning(
                "Best season match %r has low similarity (%.2f) — skipping.",
                scored[0][2]["name"], scored[0][0],
            )
        return [r for _, _, r in kept]

    # Pass 1: results that explicitly name the season.
    with_token = [
        r for r in results
        if any(pat.search(r["name"]) for pat in season_patterns)
    ]
    candidates = _ranked_above(with_token, 0.3)

    # Pass 2 (season 1 only): year-only entries like "Ted (2024)" that
    # AudioVault uses for shows not yet split into numbered seasons. Any
    # result whose name carries *any* season marker is excluded here so a
    # "Show S2" entry can't masquerade as a season-1 candidate.
    if season == 1:
        without_token = [
            r for r in results
            if not any_season_marker.search(r["name"])
        ]
        pass2 = _ranked_above(without_token, 0.4)
        if pass2:
            logger.info("Season 1: also queued %d year-only fallback(s).", len(pass2))
        candidates = candidates + pass2

    if start_year is not None:
        # Only with a series year is the top-ranked candidate trustworthy
        # enough to anchor on. Without one there is no signal separating a
        # show from its reboot, and anchoring on the title/quality winner
        # could silently exclude the right season instead of merely wasting a
        # download on the wrong one.
        candidates = _lock_to_release_year(candidates)

    if not candidates:
        logger.warning("No season %d candidates found for %r.", season, title)

    return candidates


def find_movie(results: list[dict], title: str, year: str) -> list[dict]:
    """
    Return all results from *results* that plausibly match *title* (and
    optionally *year*), ranked by score (best first, human variants before
    TTS within a tie).

    Non-English narrations are rejected, and when *year* is known a candidate
    whose parenthesised release year disagrees is rejected too — AudioVault
    carries remakes and sequels under near-identical titles, and a wrong-year
    download burns a daily download slot before alignment can reject it.

    The caller should try each candidate in order, stopping on the first that
    aligns above the score threshold.
    """
    title_lower = title.lower()
    scored: list[tuple[float, int, dict]] = []

    for result in results:
        name = result["name"]
        if _foreign_narration(name):
            continue
        name_years = _PAREN_YEAR_RE.findall(name)
        if year and name_years and year not in name_years:
            continue  # conflicting release year — wrong film or wrong sequel
        score = _title_similarity(
            title_lower, _BRACKET_TAG_RE.sub(" ", name).strip().lower()
        )
        if year and year in name_years:
            score += 0.15  # small bonus for year match

        scored.append((score, _variant_quality(name), result))

    scored.sort(key=lambda x: (round(x[0], 2), x[1]), reverse=True)
    kept = [(s, q, r) for s, q, r in scored if s >= 0.3]

    for s, _, r in kept:
        logger.info("Movie candidate: %r (score %.2f)", r["name"], s)

    if scored and not kept:
        logger.warning(
            "Best movie match %r has low similarity (%.2f) — skipping.",
            scored[0][2]["name"], scored[0][0],
        )

    return [r for _, _, r in kept]


# ------------------------------------------------------------------
# Episode extraction
# ------------------------------------------------------------------

def extract_episode(zip_path: Path, extract_dir: Path, episode: int) -> Optional[Path]:
    """
    Extract *zip_path* into *extract_dir* (if not already done) and return
    the audio file for *episode*.

    Episode matching tries several patterns in order:
      1. Explicit SxxEnn or Exx pattern in the filename.
      2. epNN or episodeNN pattern.
      3. Positional fallback (nth audio file sorted lexicographically).
    """
    # If the "file" is actually already an MP3/audio, return it directly.
    if zip_path.suffix.lower() in _AUDIO_EXTS:
        return zip_path

    _ensure_extracted(zip_path, extract_dir)

    # Natural sort so the positional fallback orders files numerically
    # (Track 1, Track 2, …, Track 10) rather than lexicographically
    # (Track 1, Track 10, Track 11, …, Track 2). Previously episode 2's
    # positional fallback picked "Track 10.mp3" when the regex patterns
    # below didn't match.
    audio_files = _natsort_paths(
        f for f in extract_dir.rglob("*") if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
    )

    if not audio_files:
        logger.error("No audio files found after extracting %s.", zip_path.name)
        return None

    # Pattern list, tried in order.
    patterns = [
        re.compile(rf"[Ee]{episode:02d}(?!\d)"),
        re.compile(rf"[Ee]{episode}(?!\d)"),
        re.compile(rf"[Ee]p(?:isode)?\.?\s*0*{episode}(?!\d)", re.IGNORECASE),
        # AudioVault disc-track format: "01 - 07 Title.mp3" where the second
        # number is the episode. Anchored to stem start to avoid false matches
        # against episode numbers embedded in titles.
        re.compile(rf"^\d+\s*-\s*0*{episode}(?!\d)"),
        # AudioVault season.episode disc format: "4.09 Title.mp3" (season 4,
        # episode 9). Anchored to stem start; the dot separates season from a
        # zero-padded episode. Without this the positional fallback was the
        # only thing matching these and could pick the wrong file.
        re.compile(rf"^\d+\.0*{episode}(?!\d)"),
    ]

    for audio in audio_files:
        for pattern in patterns:
            if pattern.search(audio.stem):
                logger.info("Matched episode %02d → %s", episode, audio.name)
                return audio

    # Positional fallback (1-based). Episode 0 is excluded: it means "special
    # episode", and positional index -1 would be meaningless; the filename
    # patterns above must match explicitly for specials.
    if episode == 0:
        logger.error(
            "Episode 00 (special) not found — the zip filename must contain E00 or similar."
        )
        return None

    if 1 <= episode <= len(audio_files):
        chosen = audio_files[episode - 1]
        logger.warning(
            "No filename match for E%02d; using positional fallback → %s",
            episode,
            chosen.name,
        )
        return chosen

    logger.error("Episode %02d not found among %d audio files.", episode, len(audio_files))
    return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _ensure_extracted(zip_path: Path, extract_dir: Path) -> None:
    """Extract *zip_path* into *extract_dir* only if not already done.

    Each entry's resolved destination is checked to be inside *extract_dir*
    before extraction (zip-slip defence), and only audio entries are written
    to disk to avoid wasting space on bundled cover art / readmes.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)

    marker = extract_dir / ".extracted"
    if marker.exists():
        return

    logger.info("Extracting %s → %s", zip_path.name, extract_dir)
    extract_root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            if Path(member.filename).suffix.lower() not in _AUDIO_EXTS:
                continue
            target = (extract_dir / member.filename).resolve()
            try:
                target.relative_to(extract_root)
            except ValueError:
                logger.warning(
                    "Refusing to extract %r — escapes %s.", member.filename, extract_root
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(65_536)
                    if not chunk:
                        break
                    dst.write(chunk)

    marker.touch()


_STOPWORDS = frozenset({"the", "a", "an", "and", "of", "in", "to", "for", "season", "series"})

# Parenthesised "(YYYY)" release-year tokens are catalog metadata and are
# removed before tokenising. A BARE year-like number is title content and is
# kept — "2012", "Wonder Woman 1984", "Blade Runner 2049", the show "1899".
# The old strip-any-year-token rule collapsed those titles into their
# neighbours ("Blade Runner 2049" → "Blade Runner") and let "2012 (2009)"
# score a perfect match against the movie "Us" once a bracket tag kept the
# token set non-empty.
_PAREN_YEAR_RE = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")

# A catalogue can date the same season a year either side of its air date
# (air year vs upload year), so the walk tolerates that much drift before it
# treats a candidate as a different work. A reboot sharing its parent's title
# sits a decade or more away and is well clear of this.
_SEASON_YEAR_LOCK_GAP = 2


def _release_year(name: str) -> Optional[int]:
    """The last parenthesised year in *name*, or None if it carries none.

    Last rather than first: a title can contain its own year ("Gossip Girl -
    Season 2 (2008)" has one, but "1917 (2019) [US]" has two and only the
    trailing one is catalogue metadata."""
    years = _PAREN_YEAR_RE.findall(name)
    return int(years[-1]) if years else None


def _lock_to_release_year(candidates: list[dict]) -> list[dict]:
    """Drop candidates dated more than ``_SEASON_YEAR_LOCK_GAP`` years from the
    best-ranked one.

    Every AudioVault variant of one season shares a year, so this leaves the
    human/TTS walk intact while stopping a fallback onto a different show that
    happens to share a title and a season number — the live case being a 2007
    series' Season 2 walking onto its 2021 reboot's Season 2 and spending a
    665 MB download on it. Candidates without a year are always kept: absent
    metadata is not evidence of a different work.
    """
    if not candidates:
        return candidates
    anchor = _release_year(candidates[0]["name"])
    if anchor is None:
        return candidates
    kept = []
    for r in candidates:
        year = _release_year(r["name"])
        if year is None or abs(year - anchor) <= _SEASON_YEAR_LOCK_GAP:
            kept.append(r)
        else:
            logger.info(
                "Dropping season candidate %r — dated %d against %d, a different work.",
                r["name"], year, anchor,
            )
    return kept


# Roman numerals 1-20 cover almost every theatrical sequel naming convention
# (Rocky I-V, Final Destination II, Saw V/VI/VII/VIII, etc.). Normalising
# these to digits BEFORE the sequel-mismatch guard runs catches the
# "Rocky II vs Rocky V" class of misrouting that the digit-only check
# previously missed entirely.
_ROMAN_TO_INT = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
    "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
}


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on word tokens with sequel-aware digit handling.

    Parenthesised release years are removed before tokenising (metadata
    noise: ``Charmed (1998)`` vs ``Charmed - Season 8 (2005)`` shouldn't be
    penalised for disagreeing on a year). Every other digit — including a
    bare year-like one — is kept as title content. If both titles carry
    title-meaningful digits and *no* digit is shared, the score is
    hard-capped — that's what stops ``Iron Man 2`` and ``Iron Man 3`` from
    collapsing to 1.0 and routing the wrong sequel's audio to a Radarr grab.

    TV's ``find_season`` already filters candidates by season token before
    calling this, so a per-season number difference here is only ever a
    real sequel signal (or noise we don't care about because the candidate
    pool already agrees on the season).
    """
    def tokenize(s: str) -> set[str]:
        s = _PAREN_YEAR_RE.sub(" ", s)
        s = re.sub(r"[^\w\s]", " ", s.lower())
        # Normalise roman numerals to digits so the sequel-mismatch guard
        # below can catch "Rocky II vs Rocky V" the same way it catches
        # "Iron Man 2 vs Iron Man 3". Only converts tokens that are
        # exclusively roman numerals — a real word like "I" is in
        # _STOPWORDS so it gets dropped anyway. "V" alone (not in
        # stopwords) becomes "5", which is the desired behaviour for a
        # title token meaning "fifth in the series."
        return {_ROMAN_TO_INT.get(t, t) for t in s.split() if t not in _STOPWORDS}

    tokens_a = tokenize(a)
    tokens_b = tokenize(b)

    if not tokens_a or not tokens_b:
        return 0.0

    base = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

    # Sequel guard: when both titles have digit tokens AND no digit is
    # shared, this is almost certainly the wrong sequel. Cap below the
    # caller's 0.3 threshold so they're rejected even when every word
    # matches.
    digits_a = {t for t in tokens_a if t.isdigit()}
    digits_b = {t for t in tokens_b if t.isdigit()}
    if digits_a and digits_b and not (digits_a & digits_b):
        return min(base, 0.25)

    return base
