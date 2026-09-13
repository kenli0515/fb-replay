#!/usr/bin/env python3
"""Build data/matches.json for the fb-replay page.

Sources:
  1. NOW TV (sports.now.com) free match highlights for Hong Kong — official and
     legal, but its own Chinese titles carry the result, so they are scrubbed.
  2. YouTube highlight search, one verified link per fixture.

Every string that comes from a source passes through sanitize() before it is
written, and audit() refuses to publish anything that still looks like a score.

Examples:
  python3 scripts/build_data.py
  python3 scripts/build_data.py --areas england,ucl,spain --limit 12
  python3 scripts/build_data.py --no-youtube --limit 20
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import math
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HK_TZ = zoneinfo.ZoneInfo("Asia/Hong_Kong")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

API_BASE = "https://sportsapi.now.com/api/getSoccerVideoList"
PAGE_BASE = "https://sports.now.com/home/video"

# area hash -> (display name, searchTagsKey)
AREAS = {
    "england": ("英超", "soccerEnglandSearchTags"),
    "ucl": ("歐聯", "soccerUCLSearchTags"),
    "uel": ("歐霸", "soccerUELSearchTags"),
    "uecl": ("歐協聯", "soccerUECLSearchTags"),
    "spain": ("西甲", "soccerSpainSearchTags"),
    "italy": ("意甲", "soccerItalySearchTags"),
    "germany": ("德甲", "soccerGermanySearchTags"),
    "local": ("本地足球", "soccerHkSearchTags"),
    "otherSoccer": ("其他足球", "soccerOtherSearchTags"),
    "worldcup2026": ("世界盃 2026", "worldcup2026SearchTags"),
}
DEFAULT_AREAS = ["england", "ucl"]

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_SPECIAL_FOLD = str.maketrans(
    {
        "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss",
        "đ": "d", "Đ": "d", "ł": "l", "Ł": "l", "œ": "oe",
        "Œ": "oe", "ı": "i", "ħ": "h", "ŧ": "t",
    }
)

STOP_TOKENS = {
    "fc", "cf", "afc", "sc", "ac", "ss", "ssc", "cd", "ud", "cfc",
    "club", "the", "de", "of", "and", "vs", "v", "sport", "sports",
    "football", "soccer", "ii",
}


def ascii_fold(value: str) -> str:
    value = value.translate(_SPECIAL_FOLD)
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", ascii_fold(value).lower())


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


# ---------------------------------------------------------------------------
# Spoiler scrubbing
# ---------------------------------------------------------------------------

# `\w` matches CJK too, so digit boundaries are used instead of word boundaries:
# a score written as 「新特蘭0:2阿仙奴」 has to be stripped as well.
_SCORE_DASH_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})(?!\d)")
_SCORE_COLON_RE = re.compile(r"(?<!\d)(\d)\s*[:\uff1a]\s*(\d)(?!\d)")
_SCORE_COLON_CJK_RE = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])(\d{1,2})\s*[:\uff1a]\s*(\d{1,2})"
    r"(?=[\u3400-\u4dbf\u4e00-\u9fff])"
)
_ANY_SCORE_RES = (_SCORE_DASH_RE, _SCORE_COLON_RE, _SCORE_COLON_CJK_RE)
_BOUNDED_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_NUMERIC_DATE_RE = re.compile(r"(?<!\w)\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\w)")
_TEXT_DATE_RE = re.compile(r"(?<!\w)(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})(?!\w)")
_SEASON_RE = re.compile(r"(?<![\d/])\d{2,4}[/-]\d{2}(?![\d/])")
_YEAR_RE = re.compile(r"(?<![\d/])(20\d{2})(?![\d/])")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Dates and season markers are stripped before scores are looked for, so that
# "9/12/2026" is not mistaken for a result and "2 - 1" is not mistaken for a date.
_DATE_RES = (_BOUNDED_DATE_RE, _NUMERIC_DATE_RE, _TEXT_DATE_RE, _SEASON_RE)
_RESULT_WORD_RE = re.compile(
    r"^\s*(win|wins|defeat|defeats|loss|draw|draws|victory|beat|beats|edge|"
    r"thrash|thrashes|humiliat\w*|stun\w*|comeback|equaliser|equalizer)",
    re.I,
)


def _score_replacement(match: re.Match) -> str:
    tail = match.string[match.end():match.end() + 20]
    if _RESULT_WORD_RE.match(tail):
        return " "
    before = match.string[max(0, match.start() - 32):match.start()]
    if re.search(r"\bvs\b|\bv\b|versus", before, re.I):
        return " "
    return " vs "


def _tidy(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,;])(?=\s|$)", r"\1", value)
    value = re.sub(r"([(\[])\s*([)\]])", "", value)
    value = re.sub(r"\bvs\b(\s+vs\b)+", "vs", value, flags=re.I)
    value = re.sub(r"^[\s\-\u2013\u2014|:,.]+", "", value)
    value = re.sub(r"[\s\-\u2013\u2014|:,.]+$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def sanitize(value: str) -> str:
    """Remove score-like tokens from source text."""
    value = clean_text(value)
    value = _BOUNDED_DATE_RE.sub(" ", value)
    for pattern in _ANY_SCORE_RES:
        value = pattern.sub(_score_replacement, value)
    return _tidy(value)


SPOILER_WORDS = [
    "win", "wins", "won", "winner", "beaten", "defeat", "defeats", "loss",
    "lost", "draw", "draws", "drew", "victory", "hero", "heroics", "screamer",
    "stunner", "masterclass", "dominant", "thrash", "humiliat", "destroy",
    "stun", "shock", "upset", "chaos", "drama", "penalt", "eliminat", "qualif",
    "progress", "advance", "clinch", "seal", "seals", "sends", "celebrat",
    "comeback", "equaliser", "equalizer", "collapse", "hammer", "rout",
    "smash", "sink", "dump", "unbeaten", "goal", "goals", "score", "scores",
    "scored", "scoring", "referee", "var", "silence", "silences", "rocket",
    "worldie", "banger", "golazo", "gutted", "riot", "tame", "tames", "haunt",
    "haunts", "handled", "steals", "snatch", "snatches", "salvage",
]
_SPOILER_WORD_RE = re.compile(r"\b(" + "|".join(SPOILER_WORDS) + r")\b", re.I)

LOW_QUALITY_PHRASES = [
    "reaction", "reacts", "review", "preview", "podcast", "interview",
    "watch along", "watchalong", "fan cam", "fancam", "vlog", "analysis",
    "tactical", "prediction", "live stream", "post match", "pre match",
    "press conference", "matchday vlog", "fan reaction", "fanzone", "fan zone",
]

NEUTRAL_VOCAB = {
    "highlights", "highlight", "extended", "full", "match", "matches", "replay",
    "replays", "video", "official", "premier", "league", "uefa", "champions",
    "cup", "europa", "conference", "efl", "fa", "laliga", "serie", "bundesliga",
    "ligue", "derby", "all", "every", "best", "moments", "show", "shows", "tv",
    "hd", "watch", "online", "stream", "football", "soccer", "sport", "sports",
    "round", "leg", "stage", "season", "final", "semi", "quarter", "playoff",
    "fixture", "recap", "game", "games", "part", "minutes", "minute", "epl",
    "ucl", "group", "away", "home",
}


def spoiler_hits(value: str) -> list[str]:
    folded = ascii_fold(value).lower()
    return sorted({match.group(1) for match in _SPOILER_WORD_RE.finditer(folded)})


def editorial_words(raw_title: str, home: str, away: str) -> list[str]:
    allowed = set(NEUTRAL_VOCAB)
    for team in (home, away):
        allowed.update(tokenize(team))
    return sorted(
        token
        for token in set(tokenize(raw_title))
        if len(token) >= 3 and not token.isdigit() and token not in allowed
    )


def season_start(when: datetime.date) -> int:
    """First year of the season a date belongs to (July is the boundary)."""
    return when.year if when.month >= 7 else when.year - 1


def title_dates(value: str) -> list[datetime.date]:
    """Every date a title might be stating, read day-first and month-first."""
    found: list[datetime.date] = []
    for match in _BOUNDED_DATE_RE.finditer(value):
        try:
            found.append(datetime.date.fromisoformat(match.group(0)))
        except ValueError:
            continue
    for match in _NUMERIC_DATE_RE.finditer(value):
        first, second, year = (int(part) for part in re.split(r"[-/.]", match.group(0)))
        if year < 100:
            year += 2000
        for day, month in ((first, second), (second, first)):
            try:
                found.append(datetime.date(year, month, day))
            except ValueError:
                continue
    for match in _TEXT_DATE_RE.finditer(value):
        month = _MONTHS.get(match.group(2)[:3].lower())
        if not month:
            continue
        try:
            found.append(datetime.date(int(match.group(3)), month, int(match.group(1))))
        except ValueError:
            continue
    return found


def looks_like_score(value: str) -> bool:
    """True when the text states a result, even without a separator: 'CITY 3 ALBION 0'."""
    if remaining_scores(value):
        return True
    stripped = value
    for pattern in _DATE_RES:
        stripped = pattern.sub(" ", stripped)
    numbers = [token for token in tokenize(stripped) if token.isdigit() and len(token) <= 2]
    return len(numbers) >= 2


def remaining_scores(*values: str) -> list[str]:
    """Anything score-like still visible in the finished data."""
    found = []
    for value in values:
        if not value:
            continue
        for pattern in _ANY_SCORE_RES:
            found.extend(match.group(0) for match in pattern.finditer(value))
    return found


# ---------------------------------------------------------------------------
# NOW TV
# ---------------------------------------------------------------------------

_BRACKET_RE = re.compile(r"^【[^】]*】\s*")
_TAIL_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_VS_RE = re.compile(r"\s+(?:vs\.?|v\.?|versus)\s+", re.I)
_NAME_SCORE_RE = re.compile(r"([A-Za-z][A-Za-z .'\-]*?)\s*\d{1,2}\s*-\s*\d{1,2}\s*([A-Za-z][A-Za-z .'\-]*)")
# NOW TV joins its own prefixes with dashes, sometimes without a space: only
# dashes that have whitespace on one side are separators, so "Saint-Germain" or
# "Bodo/Glimt" survive intact.
_NAME_SPLIT_RE = re.compile(r"\s+-\s*|\s*-\s+")
_LEAGUE_PREFIX_RE = re.compile(
    r"^(?:mbts(?:\s+only)?|UCL|UEL|UECL|PL|LaLiga|Ligue\s*1|Serie\s*A|Bundesliga)"
    r"\b[\s\-]*",
    re.I,
)
JUNK_TEAM_WORDS = (
    "summit", "preview", "museum", "opening", "award", "show", "life",
    "highlights", "mbts", "only", "final" ,"press", "interview", "ceremony",
)


def fetch_json(url: str, timeout: int) -> list:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _split_cjk_score(text: str):
    """'【英超】新特蘭0:2阿仙奴' -> ('新特蘭', '阿仙奴') without keeping the score."""
    for pattern in (_SCORE_COLON_CJK_RE, _SCORE_COLON_RE, _SCORE_DASH_RE):
        match = pattern.search(text)
        if match is None:
            continue
        home = _BRACKET_RE.sub("", text[: match.start()]).strip(" 　-–—|:,")
        away = text[match.end():].strip(" 　-–—|:,")
        if home and away:
            return home, away
    return None, None


def _parse_english_name(video_name: str):
    text = _TAIL_BRACKET_RE.sub("", clean_text(video_name)).strip()
    text = _NAME_SPLIT_RE.split(text)[-1].strip()
    text = _LEAGUE_PREFIX_RE.sub("", text).strip()
    parts = _VS_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        home, away = (part.strip(" -–—|:,") for part in parts)
        if home and away and not any(word in home.lower() for word in JUNK_TEAM_WORDS):
            return home, away
        if home and away:
            return None, None
    match = _NAME_SCORE_RE.search(text)
    if match:
        home = match.group(1).strip(" -–—|:,")
        away = match.group(2).strip(" -–—|:,")
        if home and away:
            return home.title(), away.title()
    return None, None


def parse_nowtv_item(item: dict, area: str) -> dict | None:
    raw_desc = clean_text(item.get("videoDescChi") or "")
    home_zh, away_zh = _split_cjk_score(raw_desc)
    home_en, away_en = _parse_english_name(item.get("videoName") or "")
    if not (home_zh and away_zh) and not (home_en and away_en):
        return None

    stamp = item.get("onAirDate") or item.get("publishDate")
    kickoff = None
    if stamp:
        kickoff = datetime.datetime.fromtimestamp(stamp / 1000, tz=HK_TZ).date()
    league, _ = AREAS[area]
    duration = item.get("videoDuration")
    try:
        seconds = int(float(duration)) * 60
    except (TypeError, ValueError):
        seconds = None
    return {
        "area": area,
        "league": league,
        "date": kickoff.isoformat() if kickoff else None,
        "home": home_en or home_zh,
        "away": away_en or away_zh,
        "homeZh": home_zh or home_en,
        "awayZh": away_zh or away_en,
        "titleZh": sanitize(raw_desc) or None,
        "videoId": item.get("tvVideoId"),
        "nowtvUrl": f"{PAGE_BASE}#tvVideoId={item.get('tvVideoId')}&type={area}",
        "durationSeconds": seconds,
        "publishedAt": (
            datetime.datetime.fromtimestamp(stamp / 1000, tz=HK_TZ).isoformat()
            if stamp
            else None
        ),
        "youtube": None,
    }


def nowtv_fixtures(areas: list[str], page_size: int, timeout: int) -> list[dict]:
    fixtures = []
    for area in areas:
        name, tag = AREAS[area]
        url = f"{API_BASE}?searchTagsKey={urllib.parse.quote(tag)}&pageSize={page_size}&pageNo=1"
        try:
            items = fetch_json(url, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            print(f"warning: NOW TV area {name} failed ({error})", file=sys.stderr)
            continue
        for item in items or []:
            fixture = parse_nowtv_item(item, area)
            if fixture:
                fixtures.append(fixture)
    fixtures.sort(key=lambda fixture: fixture["publishedAt"] or "", reverse=True)
    for index, fixture in enumerate(fixtures):
        fixture["id"] = f"nowtv-{fixture['videoId']}-{index}"
    return fixtures


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

YT_SEPARATOR = "\x1f"
YT_PRINT = YT_SEPARATOR.join(
    ["%(title)s", "%(id)s", "%(channel)s", "%(duration)s", "%(view_count)s"]
)

PREFERRED_CHANNELS = [
    "sky sports", "tnt sports", "premier league", "uefa", "espn", "cbs sports",
    "dazn", "bein sports", "nbc sports", "amazon prime", "bbc", "canal+",
    "fox sports", "paramount+", "liga", "serie a", "bundesliga",
]

# YouTube's own upload-date filters, applied through the `sp` query parameter.
# Filtering server-side keeps last season's meeting out of the result set; a
# plain keyword search cannot, and the old fixture outranks a preview of a match
# that has not kicked off yet.
YT_WINDOWS = {
    "hour": "EgIIAA%3D%3D",
    "day": "EgIIAQ%3D%3D",
    "week": "EgIIAw%3D%3D",
    "month": "EgIIBA%3D%3D",
    "year": "EgIIBQ%3D%3D",
}
YT_RESULTS_URL = "https://www.youtube.com/results?search_query={query}&sp={window}"

# Live shows, previews and reaction videos are not highlights. A fixture that
# has not been played yet can only ever produce these, so they are dropped
# outright rather than merely ranked low.
NOT_A_HIGHLIGHT_PHRASES = [
    "countdown", "kick off", "kick-off", "matchday live", "match day live",
    "watch along", "watchalong", "watch-along", "watch party", "preview",
    "prediction", "predictions", "team news", "line up", "line-up", "lineup",
    "starting xi", "confirmed team", "press conference", "presser",
    "fan cam", "fancam", "fan reaction", "live stream", "live match",
    "warm up", "vlog", "simulation", "top 10", "reaction", "podcast",
    "pre-match", "pre match", "talking points", "how to watch",
    # game footage is never the match itself
    "efootball", "e-football", "gameplay", "pro evolution", "fifa 2", "fc 25",
    "fc 26", "career mode", "master league",
]
_LIVE_RE = re.compile(r"(?<![a-z])live(?![a-z])")

# Which competition the words in a title belong to, per NOW TV area. A title
# naming a different competition is about a different match.
AREA_COMPETITIONS = {
    "england": "premier league",
    "ucl": "champions league",
    "uel": "europa league",
    "uecl": "conference league",
    "spain": "la liga",
    "italy": "serie a",
    "germany": "bundesliga",
    "worldcup2026": "world cup",
}
COMPETITION_ALIASES = {
    "premier league": ("premier league", "epl"),
    "champions league": ("champions league", "ucl"),
    "europa league": ("europa league", "uel"),
    "conference league": ("conference league", "uecl"),
    "la liga": ("la liga", "laliga"),
    "serie a": ("serie a",),
    "bundesliga": ("bundesliga",),
    "world cup": ("world cup",),
}


def _word_in_text(word: str, text: str) -> bool:
    folded = ascii_fold(text).lower()
    tokens = set(tokenize(text))
    candidate = ascii_fold(word).lower()
    return candidate in tokens or (len(candidate) >= 4 and candidate in folded)


def _team_in_text(team: str, text: str) -> bool:
    tokens = [token for token in tokenize(team) if token not in STOP_TOKENS]
    distinctive = [token for token in tokens if len(token) >= 4] or tokens
    return any(_word_in_text(token, text) for token in distinctive)


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def yt_search(query: str, count: int, timeout: int, window: str | None = None) -> list[dict]:
    if window in YT_WINDOWS:
        target = YT_RESULTS_URL.format(
            query=urllib.parse.quote_plus(query), window=YT_WINDOWS[window]
        )
    else:
        target = f"ytsearch{count}:{query}"
    command = [
        "yt-dlp", "--ignore-config", "--no-warnings", "--flat-playlist",
        "--skip-download", "--socket-timeout", "15",
        "--playlist-end", str(count), "--print", YT_PRINT,
        target,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout * 3)
    results = []
    for line in completed.stdout.splitlines():
        parts = line.split(YT_SEPARATOR)
        if len(parts) < 5:
            continue
        title, video_id, channel, duration, views = (part.strip() for part in parts[:5])
        if not video_id or video_id == "NA":
            continue
        results.append(
            {
                "titleRaw": title,
                "title": sanitize(title),
                "id": video_id,
                "channel": sanitize(channel) if channel != "NA" else None,
                "duration": _to_number(duration),
                "views": _to_number(views),
            }
        )
    return results


def _yt_score(candidate: dict, fixture: dict) -> float:
    score = 0.0
    raw = candidate["titleRaw"]
    if _team_in_text(fixture["home"], raw):
        score += 3.0
    if _team_in_text(fixture["away"], raw):
        score += 3.0
    lowered = raw.lower()
    if "highlight" in lowered:
        score += 2.0
    if "extended" in lowered:
        score += 0.5
    score -= 0.5 * min(len(spoiler_hits(raw)), 3)
    score -= 0.5 * min(len(editorial_words(raw, fixture["home"], fixture["away"])), 4)
    if any(phrase in lowered for phrase in LOW_QUALITY_PHRASES):
        score -= 5.0
    channel = (candidate["channel"] or "").lower()
    verified = False
    if any(name in channel for name in PREFERRED_CHANNELS):
        score += 5.0
        verified = True
    if _team_in_text(fixture["home"], channel) or _team_in_text(fixture["away"], channel):
        score += 4.0
        verified = True
    if not verified:
        score -= 1.0
    duration = candidate["duration"]
    if duration is None:
        score -= 0.5
    elif duration < 60:
        score -= 2.0
    elif duration <= 1800:
        score += 1.0
    elif duration > 5400:
        score -= 2.0
    score += 0.35 * math.log10((candidate["views"] or 0) + 1)
    return score


def hk_today() -> datetime.date:
    """Today in Hong Kong, the day a European fixture is filed under."""
    return datetime.datetime.now(HK_TZ).date()


def yt_window_for(fixture_date, today=None):
    """Upload-date filter that fits the fixture's age, or None to skip YouTube.

    A fixture that has not kicked off has no highlight, and every result YouTube
    can offer for it belongs to an earlier meeting.
    """
    today = today or hk_today()
    if fixture_date is None:
        return "week"
    age = (today - fixture_date).days
    if age < 0:
        return None
    # YouTube's "today" filter means "today in California", not "the last 24
    # hours", so a highlight posted hours after a late kick-off falls outside it.
    # A week is used instead, and the guards below keep last week's meeting out.
    if age <= 7:
        return "week"
    if age <= 31:
        return "month"
    return "year"


def not_a_highlight(raw_title: str) -> str | None:
    """The phrase showing this title is not a highlight, or None."""
    folded = " " + ascii_fold(raw_title).lower() + " "
    for phrase in NOT_A_HIGHLIGHT_PHRASES:
        if phrase in folded:
            return phrase
    if _LIVE_RE.search(folded):
        return "live"
    return None


def trusted_channel(candidate: dict, fixture: dict) -> bool:
    """True when a broadcaster or one of the two clubs posted the video."""
    channel = (candidate["channel"] or "").lower()
    if any(name in channel for name in PREFERRED_CHANNELS):
        return True
    return _team_in_text(fixture["home"], channel) or _team_in_text(fixture["away"], channel)


def competition_mismatch(raw_title: str, area: str | None) -> str | None:
    """Name the competition a title belongs to when it is not this fixture's."""
    expected = AREA_COMPETITIONS.get(area or "")
    if not expected:
        return None
    folded = ascii_fold(raw_title).lower()
    if any(alias in folded for alias in COMPETITION_ALIASES[expected]):
        return None
    for name, aliases in COMPETITION_ALIASES.items():
        if any(alias in folded for alias in aliases):
            return f"names another competition ({name})"
    return None


def stale_title(raw_title: str, fixture_date) -> str | None:
    """Point at the mismatch when a title names a date or season that is not this fixture."""
    if fixture_date is None:
        return None
    found = title_dates(raw_title)
    if found and all(abs((item - fixture_date).days) > 3 for item in found):
        return f"title dates another meeting ({found[0].isoformat()})"
    season = season_start(fixture_date)
    for match in _SEASON_RE.finditer(raw_title):
        head = match.group(0).replace("-", "/").split("/")[0]
        year = int(head) + (2000 if len(head) == 2 else 0)
        if year < season:
            return f"previous season ({head})"
    for match in _YEAR_RE.finditer(raw_title):
        if int(match.group(1)) < season:
            return f"previous season ({match.group(1)})"
    return None


def youtube_reject(candidate: dict, fixture: dict, fixture_date) -> str | None:
    """Why this candidate cannot be the highlight of this fixture, or None."""
    raw = candidate["titleRaw"]
    phrase = not_a_highlight(raw)
    if phrase:
        return f"not a highlight ({phrase})"
    if looks_like_score(raw):
        return "title states the score"
    mismatch = competition_mismatch(raw, fixture.get("area"))
    if mismatch:
        return mismatch
    return stale_title(raw, fixture_date)


def _fixture_date(fixture: dict):
    try:
        return datetime.date.fromisoformat(fixture["date"])
    except (KeyError, TypeError, ValueError):
        return None


def attach_youtube(fixture: dict, args) -> None:
    home, away = fixture["home"], fixture["away"]
    fixture["youtube"] = []
    fixture_date = _fixture_date(fixture)
    window = args.yt_window
    if window == "auto":
        window = yt_window_for(fixture_date)
    if window is None:
        if args.debug:
            print(
                f"debug: youtube skipped for {home} vs {away}: "
                f"{fixture['date']} has not kicked off yet",
                file=sys.stderr,
            )
        return
    query = f"{home} vs {away} highlights"
    try:
        candidates = yt_search(query, args.yt_search, args.timeout, window=window)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"warning: youtube lookup failed for {query} ({error})", file=sys.stderr)
        return
    verified = [
        candidate
        for candidate in candidates
        if _team_in_text(home, candidate["titleRaw"]) and _team_in_text(away, candidate["titleRaw"])
    ]
    usable = []
    for candidate in verified:
        reason = youtube_reject(candidate, fixture, fixture_date)
        if reason is None:
            usable.append(candidate)
        elif args.debug:
            print(
                f"debug:   dropping {candidate['id']} ({reason}) "
                f"channel={candidate['channel']!r} :: {candidate['titleRaw']}",
                file=sys.stderr,
            )
    if fixture_date and fixture_date >= hk_today():
        # A fixture dated today may not have kicked off yet, so a recent upload is
        # only believable from a broadcaster or one of the two clubs.
        trusted = []
        for candidate in usable:
            if trusted_channel(candidate, fixture):
                trusted.append(candidate)
            elif args.debug:
                print(
                    f"debug:   dropping {candidate['id']} (untrusted channel for a fixture "
                    f"dated today) channel={candidate['channel']!r} :: {candidate['titleRaw']}",
                    file=sys.stderr,
                )
        usable = trusted
    usable.sort(key=lambda candidate: _yt_score(candidate, fixture), reverse=True)
    if args.debug:
        print(
            f"debug: {query} [uploaded: {window}] -> {len(candidates)} results, "
            f"{len(verified)} verified, {len(usable)} usable",
            file=sys.stderr,
        )
    verified = usable
    fixture["youtube"] = [
        {
            "id": candidate["id"],
            "title": candidate["title"],
            "titleHidden": bool(spoiler_hits(candidate["titleRaw"]))
            or len(editorial_words(candidate["titleRaw"], home, away)) >= 3,
            "channel": candidate["channel"],
            "durationSeconds": candidate["duration"],
        }
        for candidate in verified[: args.yt_results]
    ]
    for (entry, candidate) in zip(fixture["youtube"], verified[: args.yt_results]):
        if entry["titleHidden"]:
            entry["title"] = None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build(args) -> dict:
    fixtures = nowtv_fixtures(args.areas, args.page_size, args.timeout)
    if args.limit:
        fixtures = fixtures[: args.limit]

    if not args.no_youtube and fixtures:
        for fixture in fixtures:
            attach_youtube(fixture, args)
    else:
        for fixture in fixtures:
            fixture["youtube"] = []

    return {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "areas": [{"key": key, "name": AREAS[key][0]} for key in args.areas],
        "sources": ["nowtv", "youtube"],
        "matches": fixtures,
    }


def audit(payload: dict) -> int:
    problems = []
    for match in payload["matches"]:
        fields = [
            match.get("home"),
            match.get("away"),
            match.get("homeZh"),
            match.get("awayZh"),
            match.get("titleZh"),
        ]
        fields += [video.get("title") for video in match.get("youtube") or []]
        hits = remaining_scores(*[field for field in fields if field])
        if hits:
            problems.append(f"nowtv-{match.get('videoId')}: {hits}")
    for problem in problems:
        print(f"score-like text found -> {problem}", file=sys.stderr)
    return len(problems)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--areas", default=",".join(DEFAULT_AREAS), help="NOW TV areas to list")
    parser.add_argument("--limit", type=int, default=12, help="max matches to publish")
    parser.add_argument("--page-size", type=int, default=48, help="items requested per area")
    parser.add_argument("--no-youtube", action="store_true")
    parser.add_argument("--yt-search", type=int, default=8, help="YouTube results examined")
    parser.add_argument("--yt-results", type=int, default=1, help="YouTube links kept")
    parser.add_argument(
        "--yt-window",
        choices=["auto", *YT_WINDOWS],
        default="auto",
        help="only consider videos uploaded in this window; auto picks by fixture age (default)",
    )
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--debug", action="store_true", help="explain why videos are dropped")
    parser.add_argument("--out", default=str(ROOT / "data" / "matches.json"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    areas = [area.strip() for area in args.areas.split(",") if area.strip() in AREAS]
    if not areas:
        print(f"error: --areas must contain one of {', '.join(AREAS)}", file=sys.stderr)
        return 2
    args.areas = areas

    payload = build(args)
    if not payload["matches"]:
        print("no matches returned; keeping the existing data file", file=sys.stderr)
        return 1
    if audit(payload):
        print("refusing to publish: score-like text survived", file=sys.stderr)
        return 2

    if not args.no_youtube and not any(match["youtube"] for match in payload["matches"]):
        print(
            "warning: YouTube returned nothing for every match (runner may be rate limited); "
            "keeping the existing data file",
            file=sys.stderr,
        )
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    videos = sum(len(match["youtube"]) for match in payload["matches"])
    print(
        f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}: "
        f"{len(payload['matches'])} matches, {videos} youtube links "
        f"({', '.join(area['name'] for area in payload['areas'])})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
