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

from .titles import (
    _TRACK_NUMBER_MAX_DIGITS, donor_episode_title, donor_names_episode, says_undescribed,
    titles_agree,
)

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

def _described_entries(results: list[dict]) -> list[dict]:
    """*results* without the catalogue entries that say they are not described.

    AudioVault files Family Guy's season 9 as "Season 9 not described": the plain
    soundtrack, which lines up almost perfectly because nothing is narrated, so
    the gate would publish it as a description (2026-10-01).
    """
    kept = []
    for r in results:
        if says_undescribed(r["name"]):
            logger.info("Skipping %r — it says it is not described.", r["name"])
        else:
            kept.append(r)
    return kept


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
    results = _described_entries(results)
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

    def _comparable(name: str) -> str:
        """The candidate's name reduced to title content.

        Bracketed tags go so all variants of a season tie on title similarity;
        quality then breaks the tie (human AD before TTS). The season marker
        goes too: ``season_patterns`` has already established the candidate is
        for this season, so the number is metadata, and leaving it in depresses
        every correct entry's score by one unpaired token — enough that a
        two-word show's own entry scored the same 0.67 as a spin-off's.
        """
        name = _BRACKET_TAG_RE.sub(" ", name)
        for pattern in season_patterns:
            name = pattern.sub(" ", name)
        return _RELEASE_PART_RE.sub(" ", name).strip().lower()

    def _ranked_above(candidates: list[dict], threshold: float) -> list[dict]:
        # Title match stays the dominant key, so a wrong show/season can never
        # be promoted over a near-exact match by quality alone. The similarity
        # is rounded so a sub-0.01 wobble between otherwise-identical titles
        # can't defeat the quality tiebreaker.
        scored: list[tuple[float, int, dict]] = []
        for r in candidates:
            comparable = _comparable(r["name"])
            # A candidate that spells out the whole show name and then adds to
            # it is a spin-off, not this show — and similarity cannot see that,
            # because it rates such a name HIGHLY for containing every word.
            extra = _extra_title_words(title_lower, comparable)
            if extra:
                logger.warning(
                    "Rejecting season candidate %r — it names a different work "
                    "(adds %s to the show's title).",
                    r["name"], ", ".join(repr(w) for w in sorted(extra)),
                )
                continue
            scored.append(
                (_title_similarity(title_lower, comparable), _variant_quality(r["name"]), r)
            )
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
        if _foreign_narration(name) or says_undescribed(name):
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

# A pack numbered like a CD: every file opens with a bare track number and the
# title ("01 Married With Cancer.mp3" … "20 Adam West High.mp3", Family Guy
# season 17). The number is the episode's only when the WHOLE pack is laid out
# so, each number once: a lone leading number in a pack named any other way is
# as likely to begin a title ("3 Acts of God").
_TRACK_PREFIX_RE = re.compile(rf"^(\d{{1,{_TRACK_NUMBER_MAX_DIGITS}}})\s+(?=[^\W\d_])")
# The highest number such a pack may start at: "00 Recap" or "01 Pilot".
_TRACK_NUMBERING_FIRST_MAX = 1

# "part 1", "pt. 2", "(part 1)" or "(2)" closing a file's name: one episode
# recorded in pieces. Family Guy's double-length "The Simpsons Guy" is filed as
# "13 - 01 … the Simpsons Guy part 1" and "13 - 01 … The simpson guy part 2".
_PART_SUFFIX_RE = re.compile(r"(?i)(?:\b(?:part|pt)\.?\s*(\d+)|\((\d+)\))\s*\)?\s*$")
# What a name sheds with its part number: "…, Part 1" and "… - Pt 2" leave a
# title, not a title and a comma.
_PART_TRAILING = " -_.(,"

# How well a donor's name agrees with the episode's title, for ranking files.
_TITLE_EXACT = 2
_TITLE_NEAR = 1
_TITLE_NONE = 0

# How many numbers either side of an episode's own the last-resort search looks.
# The catalogue slips seen so far are local: neighbours swapped, a recording
# filed one or two places off (Family Guy season 7, 2026-10-01).
_NEIGHBOUR_REACH = 2


def extract_episode(
    zip_path: Path, extract_dir: Path, episode: int, episode_title: str = "",
    series_title: str = "",
) -> Optional[Path]:
    """
    Extract *zip_path* into *extract_dir* (if not already done) and return
    the audio file for *episode*.

    Episode matching tries several patterns in order:
      1. Explicit SxxEnn or Exx pattern in the filename.
      2. epNN or episodeNN pattern.
      3. A bare track number, in a pack that numbers every file that way.
      4. Positional fallback (nth audio file sorted lexicographically).

    The positional fallback is refused when *episode_title* and the chosen
    file's own title are both known and disagree: a zip that lacks the episode
    shifts every later file up one place. This Is Us S02E06 "The 20's"
    (2026-09-25) had no file, so the fallback handed over "2.07 The Most
    Disappointed Man" and spent an alignment on the wrong episode.
    *series_title* lets a file that repeats the show's name ("Family Guy -
    Baking Bad") be read as titled by the rest.

    This is the number's pick alone; :func:`episode_donor_options` adds the
    alternatives a title or a split recording offers.
    """
    if zip_path.suffix.lower() in _AUDIO_EXTS:
        return zip_path
    audio_files = _extracted_audio(zip_path, extract_dir)
    if not audio_files:
        return None
    numbered = _numbered(audio_files, episode)
    if numbered:
        logger.info("Matched episode %02d → %s", episode, numbered[0].name)
        return numbered[0]
    return _positional(audio_files, episode, episode_title, series_title)


def episode_donor_options(
    zip_path: Path, extract_dir: Path, episode: int, episode_title: str = "",
    series_title: str = "",
) -> list[tuple[Path, ...]]:
    """Every way one season pack can describe *episode*, best first.

    Each option is a tuple of files: one file, or the parts of one recording
    to be joined in order. The caller aligns them in turn and the acceptance
    gate judges each, so an option only decides what is TRIED:

    * the number's pick (:func:`extract_episode`), or, when every file the
      number picks is a numbered part, all the parts — "The Simpsons Guy" is
      one 44-minute episode recorded as two halves under one number;
    * ahead of it, a file elsewhere in the pack whose title agrees better with
      *episode_title* than the number's pick does. A catalogue can number a
      season differently from the library: AudioVault's Family Guy season 12
      files "Fresh Heir" as E13, which is the library's E14, and its "Season
      13 UK" swaps E02 and E04 (2026-10-01). The number's pick stays as the
      fallback, because a catalogue's own title can be misspelt beyond
      recognition ("Stewie Chris and Steve's excellent adventure" is S13E07).
    """
    if zip_path.suffix.lower() in _AUDIO_EXTS:
        return [(zip_path,)]
    audio_files = _extracted_audio(zip_path, extract_dir)
    if not audio_files:
        return []
    numbered = _numbered(audio_files, episode)
    if numbered:
        parts = _split_recording(numbered)
        primary = parts or (numbered[0],)
        if parts:
            logger.info(
                "Matched episode %02d → %d parts: %s",
                episode, len(parts), ", ".join(p.name for p in parts),
            )
        else:
            logger.info("Matched episode %02d → %s", episode, numbered[0].name)
    else:
        positional = _positional(audio_files, episode, episode_title, series_title)
        primary = (positional,) if positional else ()
    options = [primary] if primary else []
    # The number's file can be part 1 of a recording whose later parts are
    # filed under the next numbers; the files say so themselves, no title
    # needed (see :func:`_consecutive_parts`).
    if numbered and not parts:
        consecutive = _consecutive_parts(audio_files, episode, numbered[0])
        if consecutive:
            logger.info(
                "%s continues as %s — offering the whole recording.",
                numbered[0].name, ", ".join(p.name for p in consecutive[1:]),
            )
            options.insert(0, consecutive)
    if not episode_title:
        return options
    primary_level = (
        _title_agreement(episode_title, primary[0].name, series_title) if primary else _TITLE_NONE
    )
    titled, titled_level = _best_title_match(
        audio_files, episode_title, series_title, exclude=set(primary),
    )
    if titled is not None and titled_level > primary_level:
        if primary:
            logger.info(
                "%s agrees better with the title %r than %s does — trying it before "
                "the number's pick.",
                titled.name, episode_title, primary[0].name,
            )
        else:
            logger.info("No number match for E%02d; %s is titled as %r.",
                        episode, titled.name, episode_title)
        options.insert(0, (titled,))
    # The parts of one recording can be filed under consecutive numbers too: the
    # "AudioVault Original" season 9 has "[S09.E01] And Then There Were Fewer Pt
    # 1" and "[S09.E02] ... Pt 2" for the library's one 49-minute S09E01. Sonarr
    # titles a two-parter's episodes "What You Leave Behind (1)" and "(2)": the
    # parts are titled as the whole, so when nothing is titled as the title
    # given, its own part number is set aside; a title that says part 2 or
    # later is never the whole recording. The title as given goes first because
    # a bracketed number can be part of it: "Flashback (1990)" is no part 1990.
    whole_title, title_part = _title_less_part(episode_title)
    titled_parts = _title_parts(audio_files, episode_title, series_title)
    if not titled_parts and title_part == 1:
        titled_parts = _title_parts(audio_files, whole_title, series_title)
    if titled_parts and titled_parts not in options:
        logger.info("%d parts are titled as %r: %s.", len(titled_parts), episode_title,
                    ", ".join(p.name for p in titled_parts))
        options.insert(0, titled_parts)
    return options


def _title_less_part(episode_title: str) -> tuple[str, Optional[int]]:
    """*episode_title* without the part number it ends in, and that number.

    ``("What You Leave Behind", 1)`` for ``"What You Leave Behind (1)"``;
    ``(title, None)`` for a title that carries none.
    """
    m = _PART_SUFFIX_RE.search(episode_title or "")
    if not m:
        return episode_title, None
    whole = _PART_SUFFIX_RE.sub("", episode_title).rstrip(_PART_TRAILING).strip()
    return whole, int(m.group(1) or m.group(2))


def _consecutive_parts(audio_files: list[Path], episode: int, first: Path) -> tuple[Path, ...]:
    """*first* and the files numbered after it, when they are parts 1..N of one
    recording; else ``()``.

    A library can hold a feature-length finale as one file under its first
    number, where the catalogue has its halves under consecutive numbers:
    "[S07.E25] What You Leave Behind, Part 1" and "[S07.E26] What You Leave
    Behind, Part  2" for one 92-minute S07E25 (Deep Space Nine, 2026-10-10).
    Either half alone left the other 49 minutes undescribed and was refused,
    and no title said the two belonged together: a manual retry carries none.
    The files must say it themselves — part 1, then part 2 under the next
    number, titled alike — so two episodes that merely follow each other are
    never joined. Whether the video is the whole or one part is for
    :func:`workflow._whole_recording` to measure.
    """
    if _part_number(first) != 1:
        return ()
    title = _part_title(first)
    parts = [first]
    number = episode
    while True:
        number += 1
        picks = _numbered(audio_files, number)
        if len(picks) != 1 or _part_number(picks[0]) != len(parts) + 1:
            break
        if not title or titles_agree(title, _part_title(picks[0]), fuzzy=True) is not True:
            break
        parts.append(picks[0])
    return tuple(parts) if len(parts) > 1 else ()


def _part_number(audio: Path) -> Optional[int]:
    """The part number *audio*'s name ends in, or None."""
    m = _PART_SUFFIX_RE.search(audio.stem)
    return int(m.group(1) or m.group(2)) if m else None


def _part_title(audio: Path) -> str:
    """The episode title a part's name carries, less its part number."""
    whole = _PART_SUFFIX_RE.sub("", audio.stem).rstrip(_PART_TRAILING)
    return donor_episode_title(whole + audio.suffix)


def _title_parts(
    audio_files: list[Path], episode_title: str, series_title: str,
) -> tuple[Path, ...]:
    """The parts 1..N of one recording titled as *episode_title*, whatever their numbers; else ``()``.

    A file counts when its name, less its part number, agrees with the title
    (a near spelling will do: this only chooses what is aligned). Two files
    claiming the same part number make it ambiguous, and nothing is returned.
    """
    parts: dict[int, Path] = {}
    for audio in audio_files:
        m = _PART_SUFFIX_RE.search(audio.stem)
        if not m:
            continue
        whole = _PART_SUFFIX_RE.sub("", audio.stem).rstrip(_PART_TRAILING)
        if donor_names_episode(
            episode_title, whole + audio.suffix, series_title=series_title, fuzzy=True,
        ) is not True:
            continue
        number = int(m.group(1) or m.group(2))
        if number in parts:
            return ()
        parts[number] = audio
    if len(parts) < 2 or sorted(parts) != list(range(1, len(parts) + 1)):
        return ()
    return tuple(parts[n] for n in sorted(parts))


def neighbour_donors(
    zip_path: Path, extract_dir: Path, episode: int, reach: int = _NEIGHBOUR_REACH,
) -> list[Path]:
    """The files a season pack numbers near *episode*, nearest number first.

    A last resort, for when the recording catalogued for this episode turned
    out to hold another one. The "Season 7" UK pack files the library's E06 as
    "07 - 05 … The man with two Brian's" and its E05 as "07 - 07 … Tales of the
    third grade nothing" (2026-10-01), so neither the number nor the title
    leads to the right file; only aligning the neighbours finds it.

    Only a number's own filename match is offered, never a positional guess,
    and a number whose matches are a split recording's parts is passed over.
    The episode's own matches are left out: they have been tried already.
    """
    if zip_path.suffix.lower() in _AUDIO_EXTS:
        return []
    audio_files = _extracted_audio(zip_path, extract_dir)
    own = set(_numbered(audio_files, episode))
    found: list[Path] = []
    for distance in range(1, reach + 1):
        for number in (episode - distance, episode + distance):
            if number < 1:
                continue
            picks = _numbered(audio_files, number)
            if len(picks) == 1 and picks[0] not in own and picks[0] not in found:
                found.append(picks[0])
    return found


def _extracted_audio(zip_path: Path, extract_dir: Path) -> list[Path]:
    """The pack's audio files, extracted on first use, in natural order."""
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
    return audio_files


def _numbered(audio_files: list[Path], episode: int) -> list[Path]:
    """Every file whose name carries *episode*'s number, in pack order.

    The first is the number's pick; more than one only matters when they are
    the parts of one recording (see :func:`_split_recording`).
    """
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
    found = [a for a in audio_files if any(p.search(a.stem) for p in patterns)]
    if found:
        return found
    track = _track_numbers(audio_files).get(episode)
    return [track] if track else []


def _track_numbers(audio_files: list[Path]) -> dict[int, Path]:
    """Episode number → file for a pack whose every file opens with a bare
    track number, each number once; empty for a pack named any other way.

    The numbering must also start this season's count (at 0 or 1). A pack
    numbered across the whole show (season 2 opening at "27 …") counts
    episodes another way, and its "27" is this season's first episode, not
    its 27th.
    """
    numbered: dict[int, Path] = {}
    for audio in audio_files:
        m = _TRACK_PREFIX_RE.match(audio.stem)
        if not m or int(m.group(1)) in numbered:
            return {}
        numbered[int(m.group(1))] = audio
    if numbered and min(numbered) > _TRACK_NUMBERING_FIRST_MAX:
        return {}
    return numbered


def _positional(
    audio_files: list[Path], episode: int, episode_title: str, series_title: str,
) -> Optional[Path]:
    """The file at *episode*'s place in the pack, unless its title names another episode."""
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
        if donor_names_episode(
            episode_title, chosen.name, series_title=series_title, fuzzy=True,
        ) is False:
            logger.warning(
                "No filename match for E%02d; the positional fallback %s is titled %r, "
                "not %r — not using it.",
                episode, chosen.name, donor_episode_title(chosen.name), episode_title,
            )
            return None
        logger.warning(
            "No filename match for E%02d; using positional fallback → %s",
            episode,
            chosen.name,
        )
        return chosen

    logger.error("Episode %02d not found among %d audio files.", episode, len(audio_files))
    return None


def _split_recording(numbered: list[Path]) -> tuple[Path, ...]:
    """*numbered* in part order when it is one recording's parts 1..N, else ``()``."""
    if len(numbered) < 2:
        return ()
    parts: dict[int, Path] = {}
    for audio in numbered:
        m = _PART_SUFFIX_RE.search(audio.stem)
        if not m:
            return ()
        number = int(m.group(1) or m.group(2))
        if number in parts:
            return ()
        parts[number] = audio
    if sorted(parts) != list(range(1, len(parts) + 1)):
        return ()
    return tuple(parts[n] for n in sorted(parts))


def whole_recording_stem(parts: tuple[Path, ...]) -> str:
    """The name a recording's joined parts go by: the first part's, less its part number.

    ``13 - 01  Family Guy - the Simpsons Guy part 1`` gives ``13 - 01  Family
    Guy - the Simpsons Guy``, which still reads as naming the episode.
    """
    stem = _PART_SUFFIX_RE.sub("", parts[0].stem).rstrip(_PART_TRAILING)
    return stem or parts[0].stem


def _title_agreement(episode_title: str, donor_name: str, series_title: str) -> int:
    """``_TITLE_EXACT``, ``_TITLE_NEAR`` or ``_TITLE_NONE`` for how *donor_name* names the episode."""
    if donor_names_episode(episode_title, donor_name, series_title=series_title) is True:
        return _TITLE_EXACT
    if donor_names_episode(
        episode_title, donor_name, series_title=series_title, fuzzy=True,
    ) is True:
        return _TITLE_NEAR
    return _TITLE_NONE


def _best_title_match(
    audio_files: list[Path], episode_title: str, series_title: str, exclude: set[Path],
) -> tuple[Optional[Path], int]:
    """The pack's file whose title agrees best with *episode_title*, and how well.

    Exact agreement beats a near spelling; between equals the pack's first
    wins. ``(None, _TITLE_NONE)`` when no file agrees at all.
    """
    best, best_level = None, _TITLE_NONE
    for audio in audio_files:
        if audio in exclude:
            continue
        level = _title_agreement(episode_title, audio.name, series_title)
        if level > best_level:
            best, best_level = audio, level
    return best, best_level


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

# A catalogue splits one season across two uploads ("Season 1 Part 1"). That is
# release structure, not a different work, so it is stripped from a candidate's
# name alongside the season marker itself.
_RELEASE_PART_RE = re.compile(r"\b(?:part|pt)\s*\d+\b", re.IGNORECASE)

# Country qualifiers distinguish regional versions of one format ("The Office
# UK" / "The Office US"). Unlike a spin-off's subtitle they add no title
# content, so ``_extra_title_words`` does not count them — but they are left in
# the token set, so an arr app that spells the region out still scores its own
# region highest and the year lock separates the rest.
_REGION_QUALIFIERS = frozenset({"uk", "us", "usa", "au", "nz", "ca"})

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


def _title_tokens(s: str) -> set[str]:
    """Content words of a title, as the matcher compares them.

    Parenthesised release years are dropped as metadata; every other digit is
    kept as title content. Roman numerals are normalised to digits so the
    sequel-mismatch guard catches "Rocky II vs Rocky V" the same way it catches
    "Iron Man 2 vs Iron Man 3". Only tokens that are exclusively roman numerals
    convert — a real word like "I" is in _STOPWORDS so it gets dropped anyway.
    "V" alone (not in stopwords) becomes "5", which is the desired behaviour
    for a title token meaning "fifth in the series."
    """
    s = _PAREN_YEAR_RE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return {_ROMAN_TO_INT.get(t, t) for t in s.split() if t not in _STOPWORDS}


def _extra_title_words(query: str, candidate: str) -> set[str]:
    """Words *candidate* adds to a title that already contains all of *query*.

    Empty when *candidate* is not a strict superset of *query*.

    Catalogues name a spin-off by appending to its parent's title — "The Epic
    Tales of Captain Underpants **in Space**", "The Fairly OddParents**: A New
    Wish**", "Gilmore Girls **- A Year in the Life**". Symmetric Jaccard rates
    those 0.5-0.67, well clear of the 0.3 floor, precisely *because* every word
    of the query is present; and when the parent show is absent from the
    catalogue the spin-off is the only candidate, so ranking cannot save us
    either. The asymmetry is the signal: a show's own entry never carries title
    words the show's name lacks.

    The converse is left alone — a catalogue that drops a distributor prefix
    the arr app carries ("Marvel's Daredevil" → "Daredevil") is ordinary
    terseness, not a different work. Nor is a country qualifier: "The Office
    UK" is the show Sonarr calls "The Office", so ``_REGION_QUALIFIERS`` are
    not counted as added words.
    """
    query_tokens = _title_tokens(query)
    candidate_tokens = _title_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return set()
    if not query_tokens < candidate_tokens:  # strict subset ⇒ candidate adds words
        return set()
    return candidate_tokens - query_tokens - _REGION_QUALIFIERS


# Where one part of a title ends and the next begins: a colon, or a dash with
# a space either side. A dash inside a word ("Spider-Man") joins, never splits.
_TITLE_SEGMENT_RE = re.compile(r"\s*(?::|\s[-–—]\s)\s*")


def search_segments(title: str) -> list[str]:
    """The parts of *title* worth searching for on their own, longest first.

    AudioVault's search is a literal substring match, so a colon the catalogue
    writes as a dash hides every entry spelt that way: "Star Trek: Deep Space
    Nine" found seasons 1-3, 6 and 7 and not "Star Trek - Deep Space Nine -
    Season 5 (1997)" (2026-10-10), while "Deep Space Nine" finds all seven.
    A segment is what lies between colons or spaced dashes, offered when it
    has a word of three letters or more that is not a region qualifier; a
    title with no such break has no segments.
    """
    whole = title.strip()
    segments: list[str] = []
    for segment in _TITLE_SEGMENT_RE.split(whole):
        segment = segment.strip()
        words = _title_tokens(segment) - _REGION_QUALIFIERS
        if segment and segment != whole and segment not in segments \
                and any(len(word) >= 3 for word in words):
            segments.append(segment)
    return sorted(segments, key=len, reverse=True)


def names_whole_title(title: str, name: str) -> bool:
    """True when the catalogue entry *name* carries every word of *title*.

    A search on one segment of a title widens the pool to every show sharing
    those words ("The Next Generation" is Degrassi's as much as Star Trek's),
    so only an entry that spells the whole title out is handed to the matcher.
    Region qualifiers are not counted: "The Office (US)" is "The Office [US]".
    """
    wanted = _title_tokens(title) - _REGION_QUALIFIERS
    return bool(wanted) and wanted <= _title_tokens(name)


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
    tokens_a = _title_tokens(a)
    tokens_b = _title_tokens(b)

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
