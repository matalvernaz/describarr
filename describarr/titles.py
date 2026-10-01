"""
Episode titles, read from release filenames and from donor filenames.

The matcher joins a video to a donor by (show, season, episode number) alone,
and the number is not always shared: catalogues and the library can number a
season differently, and a zip's positional fallback can hand over the wrong
file. Both sides usually carry the episode title — a release is named
``Family.Guy.S21E01.Oscars.Guy.1080p...`` and its donor ``[S21.E01] Oscars
Guy.mp3`` — so the title is independent evidence of which donor describes this
episode. It is evidence, never a verdict: it chooses which file is aligned
first and can vouch for a low-scoring alignment, but the alignment decides what
is published. An unknown title (a release named
``Family.Guy.S21E11.REPACK.1080p...``, a donor named ``Track 07.mp3``) means
"no evidence", never "wrong".

When a release name carries no title, the ``.nfo`` a media server writes beside
the video usually does, so that is read as well.
"""

from __future__ import annotations

import difflib
import html
import re
import unicodedata
from pathlib import Path

# The SxxEyy token, allowing multi-episode forms (S01E01E02, S01E01-E02).
_EPISODE_TOKEN_RE = re.compile(r"(?i)\bS\d{1,3}E\d{1,4}(?:-?E\d{1,4})*\b")

# Tokens that end the title in a release name: resolution, source, service,
# codec and audio words, matched against one separator-split word whatever
# its case. None of them is an ordinary title word.
_RELEASE_WORD_RE = re.compile(
    r"(?i)^(?:\d{3,4}[pi]|4k|uhd|hdr\d*|sdr|"
    r"web|webrip|web-?dl|webdl|web-?rip|bluray|blu-?ray|bdrip|brrip|remux|hdtv|pdtv|sdtv|dvdrip|"
    r"amzn|dsnp|hulu|nf|hmax|atvp|pcok|pmtp|"
    r"repack\d*|x26[45]|h\.?26[45]|hevc|avc|av1|xvid|10bit|8bit|"
    r"aac\d*(?:\.\d)?|ac3|eac3|dd\+?\d*(?:\.\d)?|ddp\d*(?:\.\d)?|dts(?:-?hd)?|truehd|atmos|flac|opus|"
    r"multi)$"
)

# Release flags that are also ordinary words ("It", "Max", "Real",
# "Extended"): they end the title only in capitals, the way releases write
# them (``S01E13.PROPER.1080p``), so ``Mad.Max`` keeps its second word.
_RELEASE_FLAG_UPPER = frozenset({
    "PROPER", "REAL", "INTERNAL", "EXTENDED", "UNCUT", "UNRATED", "DUAL",
    "DUBBED", "SUBBED", "IT", "MAX", "CR", "STAN", "DV", "DVD", "HDR",
})


def _is_release_word(word: str) -> bool:
    return bool(_RELEASE_WORD_RE.match(word)) or word in _RELEASE_FLAG_UPPER


# Donor prefixes before the title: "[S21.E01] ", "2.07 ", "5.01,5.02 ",
# "01 - 07 ", "S08E02 ", "E07 - ".
_DONOR_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:"
    r"\[\s*S\d+\s*\.?\s*E\d+\s*\]"             # [S21.E01]
    r"|S\d+\s*E\d+(?:\s*-?\s*E\d+)*"           # S08E02, S01E01-E02
    r"|\d+\.\d+(?:\s*,\s*\d+\.\d+)*"           # 2.07, 5.01,5.02
    r"|\d+\s*-\s*\d+"                          # 01 - 07
    r"|E\d+"                                   # E07
    r")\s*[-.:_]*\s*"
)

# A donor name that is only a counter carries no title.
_GENERIC_DONOR_RE = re.compile(r"(?i)^(?:(?:track|episode|ep|part|disc|chapter|cd)\s*)?\d+$")

# A bare track number in front of the title, the way a pack numbered like a CD
# writes it: "16 You Can't Handle the Booth.mp3" (Family Guy season 17,
# 2026-10-01). It is stripped only for an extra reading, never from the literal
# one, because a title can itself begin with a number: "3 Acts of God".
_TRACK_NUMBER_MAX_DIGITS = 3
_TRACK_NUMBER_PREFIX_RE = re.compile(rf"^\d{{1,{_TRACK_NUMBER_MAX_DIGITS}}}\s+(?=[^\W\d_])")

# Trailing qualifiers on a series' name ("Archer (2009)", "Heartland (2007)
# (CA)"), which a donor that repeats the series' name leaves off.
_SERIES_QUALIFIERS_RE = re.compile(r"(?:\s*\([^)]*\))+\s*$")

# Spellings closer than this are the same title ("Brother"/"Brothers"). Numbers
# must match exactly whatever the ratio: "A Hell of a Week (1)" and "(2)" are
# 0.94 alike and different episodes.
_SIMILARITY_FLOOR = 0.9

# A leading article a release name drops: "Family.Guy.S13E02.Book.of.Joe" is
# "The Book of Joe" (matched against the folded form).
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an) ")


def episode_title_from_filename(name: str) -> str:
    """The episode title in a release filename, or ``""`` when it has none.

    Reads the words between the ``SxxEyy`` token and the first release word
    (``1080p``, ``WEB``, ``REPACK``…). ``Family Guy S20E09 The Fatman Always
    Rings Twice REPACK 1080p HULU...`` gives ``The Fatman Always Rings Twice``.
    """
    stem = Path(name).stem if Path(name).suffix.lower() in {".mkv", ".mp4", ".m4v", ".avi", ".ts"} else name
    match = _EPISODE_TOKEN_RE.search(stem)
    if not match:
        return ""
    words = []
    for word in re.split(r"[\s._]+|(?<=\w)-(?=\w)|\s-\s", stem[match.end():]):
        word = word.strip(" -")
        if not word:
            continue
        if _is_release_word(word):
            break
        words.append(word)
    return " ".join(words)


def donor_episode_title(name: str) -> str:
    """The episode title in a donor filename, or ``""`` when it names none.

    ``[S21.E01] Oscars Guy.mp3`` gives ``Oscars Guy``; ``2.07 The Most
    Disappointed Man.mp3`` gives ``The Most Disappointed Man``. Counters such
    as ``Track 07.mp3`` or LivingAudio's bare ``4.07.mp3`` give ``""``.
    """
    stem = Path(name).stem
    if _GENERIC_DONOR_RE.match(stem.strip()) or re.fullmatch(r"\d+\.\d+", stem.strip()):
        return ""
    title = _DONOR_PREFIX_RE.sub("", stem, count=1).strip()
    if not title or _GENERIC_DONOR_RE.match(title):
        return ""
    return title


def _normalise(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    folded = re.sub(r"['’`]", "", folded)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def titles_agree(a: str, b: str, *, fuzzy: bool = False) -> bool | None:
    """Whether two episode titles name the same episode.

    ``None`` when either is unknown — absence of a title is not evidence
    either way. Otherwise True for the same words after folding case, accents
    and punctuation. That exact form is what acceptance uses: "Pilot" and
    "Pilots" are 0.91 alike and can be two episodes, so a near spelling must
    never vouch for a low-scoring donor. *fuzzy* also accepts a near-identical
    spelling with the same numbers, or the same words but for a leading
    article; it is for choosing which file to align (the positional-fallback
    guard, the title search), where agreement only decides what is tried,
    never what is published.
    """
    na, nb = _normalise(a or ""), _normalise(b or "")
    if not na or not nb:
        return None
    if na == nb:
        return True
    if not fuzzy or re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return False
    if _LEADING_ARTICLE_RE.sub("", na) == _LEADING_ARTICLE_RE.sub("", nb):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _SIMILARITY_FLOOR


def donor_title_readings(name: str, series_title: str = "") -> list[str]:
    """Every way a donor filename reads as an episode title, the literal one first.

    The literal reading is :func:`donor_episode_title`. Catalogue clutter in
    front of the title gives more, each read as well as, never instead of, the
    literal one (they come back folded, the form :func:`titles_agree` compares):

    * the series' own name: ``13 - 02  Family Guy - Baking Bad.mp3`` also
      reads "baking bad";
    * a bare track number: ``16 You Can't Handle the Booth.mp3`` also reads
      "you cant handle the booth", while ``[S12.E21] 3 Acts of God.mp3`` keeps
      "3 Acts of God" as its literal reading.
    """
    literal = donor_episode_title(name)
    if not literal:
        return []
    series = _normalise(_SERIES_QUALIFIERS_RE.sub("", series_title or ""))
    readings = [literal]
    # The list grows while it is walked, so a name with both kinds of clutter
    # ("01 Family Guy - Pilot.mp3") is stripped of each in turn.
    for reading in readings:
        folded = _normalise(reading)
        without_series = (
            folded[len(series) + 1:] if series and folded.startswith(series + " ") else ""
        )
        without_number = _TRACK_NUMBER_PREFIX_RE.sub("", folded, count=1)
        for stripped in (without_series, without_number):
            if (stripped and stripped != folded and stripped not in readings
                    and not _GENERIC_DONOR_RE.match(stripped)):
                readings.append(stripped)
    return readings


def donor_names_episode(
    episode_title: str, donor_name: str, *, series_title: str = "", fuzzy: bool = False,
) -> bool | None:
    """Whether the donor file *donor_name* names the episode *episode_title*.

    True when any of :func:`donor_title_readings` agrees with the title
    (exactly, or near-identically with *fuzzy*; see :func:`titles_agree`);
    False when both are known and no reading agrees; None when either is
    unknown.
    """
    verdicts = [
        titles_agree(episode_title, reading, fuzzy=fuzzy)
        for reading in donor_title_readings(donor_name, series_title)
    ]
    if any(v is True for v in verdicts):
        return True
    if any(v is False for v in verdicts):
        return False
    return None


# The per-episode metadata a media server keeps beside a video
# ("<video stem>.nfo", Kodi's format, which Jellyfin writes). A real one is a
# few KB; a file far past this is not one and is not read.
_NFO_SUFFIX = ".nfo"
_NFO_MAX_BYTES = 1 << 20
_NFO_EPISODE_BLOCK_RE = re.compile(r"<episodedetails\b", re.IGNORECASE)
_NFO_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_NFO_SEASON_RE = re.compile(r"<season>\s*(\d+)\s*</season>", re.IGNORECASE)
_NFO_EPISODE_RE = re.compile(r"<episode>\s*(\d+)\s*</episode>", re.IGNORECASE)
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)


def episode_title_from_nfo(video_path: Path, season: int, episode: int) -> str:
    """The title in the ``.nfo`` beside *video_path*, or ``""`` when there is none to trust.

    A release name does not always carry the title (``Family Guy S12E14 1080p
    WEB-DL AAC2.0 AVC-TrollHD.mp4``) and a manual retry has no Sonarr title,
    but Jellyfin writes ``<title>Fresh Heir</title>`` beside every episode it
    has scanned. The sidecar is trusted only when it describes exactly one
    episode and its ``<season>``/``<episode>`` are *season*/*episode*: a
    multi-episode file's sidecar holds several titles, and one written under
    another numbering would lend a different episode's.
    """
    nfo = video_path.with_suffix(_NFO_SUFFIX)
    try:
        if nfo.stat().st_size > _NFO_MAX_BYTES:
            return ""
        text = nfo.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return ""
    if len(_NFO_EPISODE_BLOCK_RE.findall(text)) != 1:
        return ""
    season_m, episode_m = _NFO_SEASON_RE.search(text), _NFO_EPISODE_RE.search(text)
    if not (season_m and episode_m):
        return ""
    if (int(season_m.group(1)), int(episode_m.group(1))) != (season, episode):
        return ""
    title_m = _NFO_TITLE_RE.search(text)
    if not title_m:
        return ""
    cdata = _CDATA_RE.match(title_m.group(1))
    title = html.unescape(cdata.group(1) if cdata else title_m.group(1))
    return " ".join(title.split())
