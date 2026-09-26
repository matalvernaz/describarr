"""
Episode titles, read from release filenames and from donor filenames.

The matcher joins a video to a donor by (show, season, episode number) alone,
and the number is not always shared: catalogues and the library can number a
season differently, and a zip's positional fallback can hand over the wrong
file. Both sides usually carry the episode title — a release is named
``Family.Guy.S21E01.Oscars.Guy.1080p...`` and its donor ``[S21.E01] Oscars
Guy.mp3`` — so the title is an independent check that a donor describes this
episode. It is only ever used as a check: an unknown title (a release named
``Family.Guy.S21E11.REPACK.1080p...``, a donor named ``Track 07.mp3``) means
"no evidence", never "wrong".
"""

from __future__ import annotations

import difflib
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

# Spellings closer than this are the same title ("Brother"/"Brothers"). Numbers
# must match exactly whatever the ratio: "A Hell of a Week (1)" and "(2)" are
# 0.94 alike and different episodes.
_SIMILARITY_FLOOR = 0.9


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
    spelling with the same numbers; it is for the positional-fallback guard,
    where agreement only means "do not refuse".
    """
    na, nb = _normalise(a or ""), _normalise(b or "")
    if not na or not nb:
        return None
    if na == nb:
        return True
    if not fuzzy or re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _SIMILARITY_FLOOR
