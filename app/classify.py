"""Title cleaning (for TMDB matching) and screening labels."""

from __future__ import annotations

import re
from datetime import date

# Things theaters bolt onto a film title that are not part of it.
_SUFFIX_PATTERNS = [
    r"\s*\((?:[a-z]+ (?:with english subtitles|spoken.*|subtitled)|subtitled|dubbed|3d|imax|70mm|35mm|open caption(?:ed|s)?|oc)\)",
    r"\s*[-–—:]\s*(?:followed by|with|w/|plus)\b.*$",
    r"\s+(?:followed by|with q&a|w/ q&a|w/|with (?:director|filmmaker|panel|live)|q&a)\b.*$",
    r"\s+\b\d{1,3}(?:st|nd|rd|th) anniversary\b.*$",
    r"\s*\((?:\d{4})\)\s*$",
    r"\s+(?:in )?(?:3d|imax|70mm|35mm)\s*$",
    r"\s*\(oc\)\s*$",
]
_PREFIX_PATTERNS = [
    r"^(?:the )?met opera(?: live in hd)?\s*[:\-–]\s*",
    r"^(?:nt live|national theatre live|exhibition on screen|fathom events?|ghibli fest(?:\s+\d{4})?)\s*[:\-–]\s*",
    r"^(?:revival series|cinema sundays|midnight movie|throwback)\s*[:\-–]\s*",
]

EVENT_KEYWORDS = re.compile(
    r"\b(q\s*&\s*a|panel|discussion|met opera|opera|ballet|fathom|anniversary|"
    r"live performance|live in hd|nt live|national theatre|exhibition on screen|festival|"
    r"sing[- ]?along|quote[- ]?along|marathon|double feature|premiere screening|"
    r"in person|introduced by|presented by|special (?:event|screening)|members?'? screening|"
    r"filmmaker|director in attendance|watch party|concert|trivia)\b",
    re.IGNORECASE,
)

FORMAT_RULES = [
    ("3D", re.compile(r"\b(?:real ?d )?3d\b", re.I)),
    ("XD", re.compile(r"\bxd\b", re.I)),
    ("IMAX", re.compile(r"\bimax\b", re.I)),
    ("70mm", re.compile(r"\b70\s?mm\b", re.I)),
    ("35mm", re.compile(r"\b35\s?mm\b", re.I)),
    ("Open captions", re.compile(r"\bopen captions?\b|\(oc\)|on-screen captions", re.I)),
    ("Subtitled", re.compile(r"with english subtitles|\bsubtitled\b", re.I)),
    ("D-BOX", re.compile(r"\bd-?box\b", re.I)),
]


def clean_title(raw: str) -> str:
    title = " ".join(raw.split())
    for pattern in _PREFIX_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.I)
    for _ in range(2):
        for pattern in _SUFFIX_PATTERNS:
            title = re.sub(pattern, "", title, flags=re.I)
    title = title.strip(" -–—:")
    if title.isupper():
        title = smart_title_case(title)
    return title or raw.strip()


_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "with"}


def smart_title_case(text: str) -> str:
    words = text.lower().split()
    out = []
    for i, word in enumerate(words):
        if 0 < i < len(words) - 1 and word in _SMALL:
            out.append(word)
        elif re.fullmatch(r"[ivx]+", word) and len(word) <= 4:
            out.append(word.upper())
        else:
            out.append(re.sub(r"^([^a-z]*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), word))
    return " ".join(out)


def normalize_key(title: str) -> str:
    """Lowercase alnum key for grouping titles that failed TMDB matching."""
    title = clean_title(title).lower()
    title = re.sub(r"^(the|a|an) ", "", title)
    return re.sub(r"[^a-z0-9]+", "", title)


def is_event(raw_title: str, tags: list[str], source_flag: bool) -> bool:
    if source_flag:
        return True
    return bool(EVENT_KEYWORDS.search(raw_title)) or any(EVENT_KEYWORDS.search(t) for t in tags)


def formats(raw_title: str, tags: list[str]) -> list[str]:
    hay = " | ".join([raw_title, *tags])
    return [name for name, rx in FORMAT_RULES if rx.search(hay)]


def months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.44


def is_rerelease(release: date | None, show_day: date, min_months: int) -> bool:
    return release is not None and months_between(release, show_day) >= min_months


def is_advance(release: date | None, show_day: date) -> bool:
    return release is not None and show_day < release
