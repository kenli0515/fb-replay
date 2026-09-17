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
import os
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


def hk_today() -> datetime.date:
    """Today in Hong Kong, the day a European fixture is filed under."""
    return datetime.datetime.now(HK_TZ).date()


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
# Fixtures still to come (openfootball)
# ---------------------------------------------------------------------------

# openfootball keeps the season files of the big leagues in the open; they carry
# dates and kick-off times and, for a match nobody has played yet, no score at
# all, which makes them a safe source for a spoiler-free schedule.
SCHEDULE_BASE = "https://raw.githubusercontent.com/openfootball/football.json/master"
AREA_SCHEDULE = {
    "england": "en.1",
    "spain": "es.1",
    "italy": "it.1",
    "germany": "de.1",
}
_TEAM_SUFFIX_RE = re.compile(r"\s+(?:FC|AFC|CF|SC|AC|SV|BC|1899)$")
_TEAM_PREFIX_RE = re.compile(r"^(?:AFC|FC|CF|SC|AC|SV|BC)\s+")
_KICKOFF_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def season_slug(when: datetime.date) -> str:
    start = season_start(when)
    return f"{start}-{str(start + 1)[2:]}"


def tidy_team(name: str) -> str:
    """'Manchester United FC' and 'AFC Bournemouth' read better without the suffix."""
    name = clean_text(name)
    name = _TEAM_SUFFIX_RE.sub("", name)
    name = _TEAM_PREFIX_RE.sub("", name)
    return name.strip()


def _uk_offset(when: datetime.date) -> int:
    """Hours the UK is ahead of UTC: 1 through British Summer Time, else 0."""
    if 4 <= when.month <= 9:
        return 1
    if when.month == 3:
        last_sunday = 31 - (datetime.date(when.year, 3, 31).weekday() + 1) % 7
        return 1 if when.day >= last_sunday else 0
    if when.month == 10:
        last_sunday = 31 - (datetime.date(when.year, 10, 31).weekday() + 1) % 7
        return 1 if when.day < last_sunday else 0
    return 0


def kickoff_at(when: datetime.date, clock: str | None):
    """When a UK fixture starts, as a Hong Kong timestamp, or None without a time."""
    match = _KICKOFF_RE.match((clock or "").strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    start = datetime.datetime(when.year, when.month, when.day, hour, minute, tzinfo=HK_TZ)
    return start + datetime.timedelta(hours=8 - _uk_offset(when))


def schedule_fixtures(areas: list[str], timeout: int) -> list[dict]:
    """Every listed fixture of the running season for the areas in use."""
    slug = season_slug(hk_today())
    fixtures = []
    for area in areas:
        code = AREA_SCHEDULE.get(area)
        if not code:
            continue
        url = f"{SCHEDULE_BASE}/{slug}/{code}.json"
        try:
            payload = fetch_json(url, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            print(f"warning: schedule for {area} failed ({error})", file=sys.stderr)
            continue
        for entry in (payload or {}).get("matches") or []:
            try:
                when = datetime.date.fromisoformat(entry.get("date") or "")
            except ValueError:
                continue
            home, away = tidy_team(entry.get("team1") or ""), tidy_team(entry.get("team2") or "")
            if not (home and away):
                continue
            fixtures.append(
                {
                    "area": area,
                    "league": AREAS[area][0],
                    "date": when.isoformat(),
                    "kickoff": (
                        kickoff_at(when, entry.get("time")).isoformat()
                        if kickoff_at(when, entry.get("time"))
                        else None
                    ),
                    "home": home,
                    "away": away,
                    "homeZh": home,
                    "awayZh": away,
                    "titleZh": None,
                    "videoId": None,
                    "nowtvUrl": None,
                    "durationSeconds": None,
                    "publishedAt": None,
                    "youtube": [],
                    "previews": [],
                    "upcoming": True,
                }
            )
    return fixtures


def _pair_key(fixture: dict) -> tuple:
    return (frozenset(tokenize(fixture["home"]) + tokenize(fixture["away"])), fixture.get("date"))


def merge_schedule(fixtures: list[dict], scheduled: list[dict], days: int) -> int:
    """Add the next `days` of fixtures and give the rest their kick-off time."""
    today = hk_today()
    horizon = today + datetime.timedelta(days=days)
    known = {_pair_key(fixture) for fixture in fixtures}
    added = 0
    for entry in scheduled:
        when = datetime.date.fromisoformat(entry["date"])
        if not (today <= when <= horizon):
            continue
        if _pair_key(entry) in known:
            continue
        fixtures.append(entry)
        known.add(_pair_key(entry))
        added += 1
    lookup = {_pair_key(entry): entry for entry in scheduled}
    for fixture in fixtures:
        if fixture.get("kickoff") or not fixture.get("date"):
            continue
        when = datetime.date.fromisoformat(fixture["date"])
        for shift in (0, -1, 1):
            key = (frozenset(tokenize(fixture["home"]) + tokenize(fixture["away"])), (when + datetime.timedelta(days=shift)).isoformat())
            if key in lookup and lookup[key].get("kickoff"):
                fixture["kickoff"] = lookup[key]["kickoff"]
                break
    return added


def is_upcoming(fixture: dict) -> bool:
    """True while a match has not started, so no replay can exist for it."""
    kickoff = fixture.get("kickoff")
    if kickoff:
        return datetime.datetime.fromisoformat(kickoff) > datetime.datetime.now(HK_TZ)
    when = fixture.get("date")
    return bool(when and when > hk_today().isoformat())


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

YT_SEPARATOR = "\x1f"
YT_PRINT = YT_SEPARATOR.join(
    ["%(title)s", "%(id)s", "%(channel)s", "%(duration)s", "%(view_count)s", "%(channel_id)s"]
)


def gha_warning(message: str) -> None:
    """Report a problem as a check annotation when running on GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}")

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

# One query per kind of replay: a match with no cut highlights often still has a
# full match, an extended cut or a club upload.
REPLAY_QUERIES = (
    "{home} vs {away} highlights",
    "{home} vs {away} full match",
)

# A preview is about a match nobody has played yet, so the titles worth keeping
# talk about the fixture rather than about a result.
PREVIEW_REJECT_PHRASES = [
    "highlight", "full match", "extended", "all goals", "goals &", "every goal",
    "recap", "classic", "retro", "throwback", "rewind", "watch along",
    "watchalong", "watch-along", "watch party", "fan cam", "fancam",
    "reaction", "reacts", "simulation", "gameplay", "efootball", "e-football",
    "pro evolution", "career mode", "master league", "streaming", "live stream",
    "fifa 2", "fc 25", "fc 26", "pes 2", "pes2", "ps5", "ps4", "xbox",
]

# Wording that marks a video as fixture talk: broadcast build-up, club shows and
# the fan channels that preview a game all use at least one of these.
PREVIEW_SIGNALS = [
    "preview", "predict", "team news", "press conference", "presser",
    "build up", "build-up", "talking point", "line-up", "lineup", "line up",
    "starting xi", "how to watch", "matchday", "match day", "countdown",
    "what to expect", "pre match", "pre-match", "analysis", "breakdown",
    "scout report", "combined xi", "injury news", "squad news", "derby day",
    "head to head", "h2h", "big match", "key battles", "match pack",
    "kick off", "kick-off", "preview show", "vs prediction", "preview &",
]

# Other teams wear the same name: a WSL, academy or reserve match is a different
# fixture, and its score says nothing about the one being looked for.
SIDE_COMPETITION_PHRASES = [
    "women", "womens", "wsl", "nwsl", "uwcl", "femen", "feminine", "femenino",
    "lioness", "academy", "youth", "u18", "u19", "u21", "u23", "under 18",
    "under 19", "under 21", "under 23", "premier league 2", "pl2", "reserves",
    "reserve team", "b team",
]

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
                "channelId": parts[5].strip() if len(parts) > 5 and parts[5].strip() != "NA" else None,
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
    folded = " " + _flatten(ascii_fold(raw_title)).lower() + " "
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


def _flatten(value: str) -> str:
    """Dashes and underscores read as spaces, so 'Pre - Match View' still matches."""
    return re.sub(r"\s+", " ", re.sub(r"[-\u2013\u2014_]", " ", value))


def _team_tokens(team: str) -> list[str]:
    tokens = [token for token in tokenize(team) if token not in STOP_TOKENS]
    return [token for token in tokens if len(token) >= 4] or tokens


# "X vs Y", "X v Y", "X - Y", "X face Y": the two teams have to be named against
# each other. Both names appearing somewhere in the title is not enough, or a
# roundup that lists six clubs passes for a preview of any one of them.
_H2H_SEPARATOR = (
    r"(?:vs\.?|v\.?|versus|[-\u2013\u2014:]|face[sd]?|take[s]? on|host[s]?|"
    r"visit[s]?|meet[s]?|against)"
)


def head_to_head(raw_title: str, fixture: dict, window: int = 30) -> bool:
    """True when a title names one team against the other."""
    text = _flatten(ascii_fold(raw_title)).lower()
    for first, second in (
        (_team_tokens(fixture["home"]), _team_tokens(fixture["away"])),
        (_team_tokens(fixture["away"]), _team_tokens(fixture["home"])),
    ):
        left = "|".join(re.escape(token) for token in first)
        right = "|".join(re.escape(token) for token in second)
        if re.search(
            rf"(?:{left}).{{0,{window}}}?{_H2H_SEPARATOR}.{{0,{window}}}?(?:{right})", text
        ):
            return True
    return False


def side_competition(raw_title: str, channel: str | None) -> str | None:
    """Name the other competition when a title or channel belongs to it."""
    folded = " " + _flatten(ascii_fold(f"{raw_title} {channel or ''}")).lower() + " "
    for phrase in SIDE_COMPETITION_PHRASES:
        if phrase in folded:
            return phrase
    return None


def preview_signal(raw_title: str) -> str | None:
    """The phrase marking this title as fixture talk, or None."""
    folded = _flatten(ascii_fold(raw_title)).lower()
    for phrase in PREVIEW_SIGNALS:
        if phrase in folded:
            return phrase
    return None


def preview_score(candidate: dict, fixture: dict) -> float:
    """Rank previews: broadcasters first, then clubs, then fan channels."""
    score = 0.0
    raw = candidate["titleRaw"]
    if _team_in_text(fixture["home"], raw):
        score += 3.0
    if _team_in_text(fixture["away"], raw):
        score += 3.0
    if preview_signal(raw):
        score += 2.0
    if head_to_head(raw, fixture, window=6):
        score += 1.5
    channel = (candidate["channel"] or "").lower()
    if any(name in channel for name in PREFERRED_CHANNELS):
        score += 4.0
    elif _team_in_text(fixture["home"], channel) or _team_in_text(fixture["away"], channel):
        score += 3.0
    else:
        score += 0.5
    if _LIVE_RE.search(ascii_fold(raw).lower()):
        score -= 2.0
    duration = candidate["duration"]
    if duration is not None and duration < 60:
        score -= 2.0
    elif duration is not None and duration > 5400:
        score -= 1.0
    score -= 0.5 * min(len(spoiler_hits(raw)), 3)
    score += 0.35 * math.log10((candidate["views"] or 0) + 1)
    return score


def preview_reject(candidate: dict, fixture: dict, fixture_date) -> str | None:
    """Why this candidate is not preview content for this fixture, or None."""
    raw = candidate["titleRaw"]
    side = side_competition(raw, candidate.get("channel"))
    if side:
        return f"another competition with the same names ({side})"
    lowered = _flatten(ascii_fold(raw)).lower()
    for phrase in PREVIEW_REJECT_PHRASES:
        if phrase in lowered:
            return f"not a preview ({phrase})"
    if looks_like_score(raw):
        return "title states the score"
    mismatch = competition_mismatch(raw, fixture.get("area"))
    if mismatch:
        return mismatch
    stale = stale_title(raw, fixture_date)
    if stale:
        return stale
    if not head_to_head(raw, fixture):
        return "title does not put the two teams head to head"
    if (
        preview_signal(raw) is None
        and not trusted_channel(candidate, fixture)
        and not _team_in_text(fixture["home"], candidate["channel"] or "")
        and not _team_in_text(fixture["away"], candidate["channel"] or "")
    ):
        return "no preview wording and no club or broadcaster channel"
    return None


def attach_previews(fixture: dict, args) -> None:
    """Collect fixture talk for a match that has not kicked off yet."""
    query = f"{fixture['home']} vs {fixture['away']} preview"
    fixture["previews"] = []
    fixture_date = _fixture_date(fixture)
    try:
        candidates = yt_search(query, args.preview_search, args.timeout, window=args.preview_window)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"warning: preview lookup failed for {query} ({error})", file=sys.stderr)
        return
    verified = [
        candidate
        for candidate in candidates
        if _team_in_text(fixture["home"], candidate["titleRaw"])
        and _team_in_text(fixture["away"], candidate["titleRaw"])
    ]
    usable = []
    for candidate in verified:
        reason = preview_reject(candidate, fixture, fixture_date)
        if reason is None:
            usable.append(candidate)
        elif args.debug:
            print(
                f"debug:   dropping preview {candidate['id']} ({reason}) "
                f"channel={candidate['channel']!r} :: {candidate['titleRaw']}",
                file=sys.stderr,
            )
    usable.sort(key=lambda candidate: preview_score(candidate, fixture), reverse=True)
    if args.debug:
        print(
            f"debug: {query} [uploaded: {args.preview_window}] -> {len(candidates)} results, "
            f"{len(verified)} verified, {len(usable)} usable",
            file=sys.stderr,
        )
    fixture["previews"] = [
        {
            "id": candidate["id"],
            "title": candidate["title"],
            "channel": candidate["channel"],
            "durationSeconds": candidate["duration"],
        }
        for candidate in usable[: args.preview_results]
    ]


def youtube_reject(candidate: dict, fixture: dict, fixture_date) -> str | None:
    """Why this candidate cannot be the highlight of this fixture, or None."""
    raw = candidate["titleRaw"]
    side = side_competition(raw, candidate.get("channel"))
    if side:
        return f"another competition with the same names ({side})"
    phrase = not_a_highlight(raw)
    if phrase:
        return f"not a highlight ({phrase})"
    if looks_like_score(raw):
        return "title states the score"
    if not head_to_head(raw, fixture):
        return "title does not put the two teams head to head"
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
    """Replays for a match that has been played, previews for one that has not."""
    home, away = fixture["home"], fixture["away"]
    fixture["youtube"] = []
    fixture_date = _fixture_date(fixture)
    if is_upcoming(fixture):
        attach_previews(fixture, args)
        return
    fixture["previews"] = []
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
    candidates = []
    seen: set[str] = set()
    for template in REPLAY_QUERIES:
        query = template.format(home=home, away=away)
        try:
            found = yt_search(query, args.yt_search, args.timeout, window=window)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            print(f"warning: youtube lookup failed for {query} ({error})", file=sys.stderr)
            continue
        for candidate in found:
            if candidate["id"] in seen:
                continue
            seen.add(candidate["id"])
            candidates.append(candidate)
    query = REPLAY_QUERIES[0].format(home=home, away=away)
    if not candidates:
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
# Cups (YouTube only)
# ---------------------------------------------------------------------------

# NOW TV carries neither English cup, and openfootball keeps no cup season file,
# so the fixture list has to come from somewhere. It comes from YouTube as well:
# each competition's own channel posts one upload per tie, and that upload
# supplies the two clubs, the date and the video. Nothing on the page points at a
# third-party replay site.
CUPS = {
    "fa-cup": {"name": "足總盃", "query": "Emirates FA Cup extended highlights"},
    "carabao-cup": {"name": "聯賽盃", "query": "Carabao Cup extended highlights"},
}
DEFAULT_CUPS = ["fa-cup", "carabao-cup"]

# An upload only counts when it says which cup it is, or comes from the cup's own
# channel. Without that gate a search for one competition happily returns league
# matches between the same clubs ("NINE GOAL CLASSIC! | Chelsea v Leeds United
# extended highlights" is a Carabao tie, but nothing in the title says so).
CUP_ALIASES = {
    "fa-cup": ("fa cup", "facup"),
    "carabao-cup": ("carabao cup", "carabao", "efl cup", "league cup"),
}
CUP_CHANNELS = {
    "fa-cup": ("the emirates fa cup",),
    # the EFL channel covers every EFL competition, so only its titles count
    "carabao-cup": (),
}
# Other versions of the same cup: a different competition for this page.
CUP_SIDE_COMPETITIONS = (
    "amputee", "disability", "walking football", "powerchair", "futsal", "socca",
    "esports", "efootball", "e-football",
)

# A flat playlist does not report upload dates, and a cup tie has no schedule to
# read one from. Asking yt-dlp for the full video is not an option either: it
# needs a player request, which YouTube refuses from a shared runner address and
# which is what the flat search avoids. The watch page carries the date in its
# JSON-LD instead, so it is read straight over HTTPS.
_UPLOAD_DATE_RE = re.compile(r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})')
# Not every watch page ships the JSON-LD block, but the page data still dates the
# video: "18 May 2026" for anything old, "3 days ago" for anything new.
_DATE_TEXT_RE = re.compile(r'"(?:dateText|publishDate)":.{0,200}?"simpleText":"([^"]{3,40})"', re.S)
_RELATIVE_DATE_RE = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_RELATIVE_DAYS = {
    "second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365,
}
# "Sep 16, 2026" as well as "16 Sep 2026"
_US_TEXT_DATE_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")
_PAGE_FLAGS = (
    "ytInitialData", "videoPrimaryInfoRenderer", "publishDate", "dateText",
    "relativeDateText", "datePublished", "uploadDate", "consent",
)
CUP_WATCH_URLS = (
    "https://www.youtube.com/watch?v={video_id}&hl=en&gl=US",
    "https://m.youtube.com/watch?v={video_id}&hl=en&gl=US",
)
CUP_PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}


def page_fingerprint(page: str) -> str:
    """What a fetched page looks like, for a check annotation when it has no date."""
    found = ",".join(flag for flag in _PAGE_FLAGS if flag in page) or "none"
    return f"{len(page)} bytes, markers: {found}"


def _date_text(value: str) -> str | None:
    """'18 May 2026' or '3 days ago' read off a watch page, as an ISO day."""
    found = _DATE_TEXT_RE.search(value)
    if not found:
        return None
    text = found.group(1)
    named = _TEXT_DATE_RE.search(text)
    if named:
        month = _MONTHS.get(named.group(2)[:3].lower())
        if month:
            try:
                return datetime.date(int(named.group(3)), month, int(named.group(1))).isoformat()
            except ValueError:
                return None
    named = _US_TEXT_DATE_RE.search(text)
    if named:
        month = _MONTHS.get(named.group(1)[:3].lower())
        if month:
            try:
                return datetime.date(int(named.group(3)), month, int(named.group(2))).isoformat()
            except ValueError:
                return None
    relative = _RELATIVE_DATE_RE.search(text)
    if relative:
        days = _RELATIVE_DAYS[relative.group(2).lower()] * int(relative.group(1))
        return (hk_today() - datetime.timedelta(days=days)).isoformat()
    return None
# Words a cup title wraps around the club names: "EXTENDED HIGHLIGHTS | Man United
# v Brighton | Carabao Cup" has to leave "Man United" and "Brighton" behind.
CUP_FILLER_RE = re.compile(
    r"\b(?:extended|highlights?|full|match|matches|key|moments?|goals?|all|recap|"
    r"round|final|semi|quarter|third|fourth|first|second|leg|cup|tie|vs?|versus)\b",
    re.I,
)
# An upload about a different competition that happens to have the same clubs.
CUP_OTHER_COMPETITIONS = (
    "community shield", "super cup", "world cup", "premier league", "champions league",
    "europa league", "conference league", "la liga", "serie a", "bundesliga",
    "ligue 1", "championship", "league one", "league two", "efl trophy", "vertu",
)
_CUP_SCORE_BRACKET_RE = re.compile(r"[\(\[]\s*(\d{1,2}\s*[-\u2013\u2014]\s*\d{1,2})\s*[\)\]]")
_CUP_SCORE_TAIL_RE = re.compile(r"\s*\(?\b\d{1,2}\s*[-\u2013\u2014]\s*\d{1,2}\b\)?\s*$")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\uFE0F\u200d]"
)


_FEED_CACHE: dict[str, dict[str, str]] = {}


def _channel_dates(channel_id: str, timeout: int) -> dict[str, str]:
    """video id -> upload day, from the channel's public Atom feed."""
    if channel_id not in _FEED_CACHE:
        dates: dict[str, str] = {}
        try:
            request = urllib.request.Request(
                f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                feed = response.read().decode("utf-8", "replace")
            for entry in re.findall(r"<entry>(.*?)</entry>", feed, re.S):
                video = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
                published = re.search(r"<published>(\d{4}-\d{2}-\d{2})", entry)
                if video and published:
                    dates[video.group(1)] = published.group(1)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            gha_warning(f"channel feed {channel_id} failed ({error})")
        _FEED_CACHE[channel_id] = dates
    return _FEED_CACHE[channel_id]


def cup_upload_date(video_id: str, channel_id: str | None, timeout: int) -> str | None:
    """The day a video went up.

    A flat search reports no dates, and asking yt-dlp for the whole video needs a
    player request that YouTube refuses from a shared runner address, so the date
    is read from the watch page over plain HTTPS. The channel feed is the backup,
    although it only lists the newest fifteen uploads. None means "unknown": the
    card is still worth showing without a date.
    """
    for template in CUP_WATCH_URLS:
        url = template.format(video_id=video_id)
        try:
            request = urllib.request.Request(url, headers=CUP_PAGE_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                page = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            gha_warning(f"{url} failed ({error})")
            continue
        match = _UPLOAD_DATE_RE.search(page)
        if match:
            return match.group(1)
        found = _date_text(page)
        if found:
            return found
        gha_warning(f"{url} had no upload date ({page_fingerprint(page)})")
    if channel_id:
        return _channel_dates(channel_id, timeout).get(video_id)
    return None


def _cup_team(value: str) -> str | None:
    """Trim a title fragment down to a club name, or None when it is not one."""
    value = _CUP_SCORE_TAIL_RE.sub("", value)
    value = CUP_FILLER_RE.split(value)[0]
    value = value.strip(" \t\u00a0-\u2013\u2014|:,.'\"&()[]")
    if not 2 <= len(value) <= 32 or re.search(r"\d", value):
        return None
    if any(word in value.lower() for word in JUNK_TEAM_WORDS):
        return None
    if not 1 <= len(tokenize(value)) <= 4:
        return None
    return tidy_team(value)


def _same_team(first: str, second: str) -> bool:
    """True for 'Man Utd' and 'Man United', false for two clubs that merely
    share a word: 'Leicester City' is not 'Manchester City'."""
    left, right = ascii_fold(first).lower(), ascii_fold(second).lower()
    if left == right or left.startswith(right) or right.startswith(left):
        return True
    shared = set(tokenize(left)) & set(tokenize(right)) - STOP_TOKENS
    return any(len(token) >= 4 for token in shared) or (
        len(left) >= 3 and right.startswith(left[:3])
    )


def _find_cup_fixture(
    fixtures: list[dict], home: str, away: str, when: str | None
) -> dict | None:
    """The card an upload belongs to: the same two clubs a day either side."""
    day = datetime.date.fromisoformat(when) if when else None
    for fixture in fixtures:
        if day and fixture["date"]:
            gap = abs((datetime.date.fromisoformat(fixture["date"]) - day).days)
            if gap > 1:
                continue
        if (_same_team(fixture["home"], home) and _same_team(fixture["away"], away)) or (
            _same_team(fixture["home"], away) and _same_team(fixture["away"], home)
        ):
            return fixture
    return None


def cup_pair(raw_title: str) -> tuple[str | None, str | None]:
    """The two clubs a cup highlight title names, or (None, None)."""
    text = _EMOJI_RE.sub(" ", clean_text(raw_title))
    # "(4-0)" and "[2-1]" are scores too: make them readable to the score regex
    text = _CUP_SCORE_BRACKET_RE.sub(r" \1 ", text)
    for segment in text.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        parts = _VS_RE.split(segment, maxsplit=1)
        if len(parts) == 2:
            home, away = _cup_team(parts[0]), _cup_team(parts[1])
            if home and away:
                return home, away
        match = _NAME_SCORE_RE.search(segment)
        if match:
            home, away = _cup_team(match.group(1)), _cup_team(match.group(2))
            if home and away:
                return home, away
    match = _NAME_SCORE_RE.search(text)
    if match:
        home, away = _cup_team(match.group(1)), _cup_team(match.group(2))
        if home and away:
            return home, away
    return None, None


def cup_reject(candidate: dict, cup: str) -> str | None:
    """Why this upload cannot be a match of this cup, or None."""
    raw = candidate["titleRaw"]
    lowered = _flatten(ascii_fold(raw)).lower()
    channel = (candidate.get("channel") or "").lower()
    named = any(alias in lowered for alias in CUP_ALIASES[cup])
    official = any(name in channel for name in CUP_CHANNELS[cup])
    if not (named or official):
        return "does not say which cup it is"
    for other in CUP_SIDE_COMPETITIONS:
        if other in lowered:
            return f"another version of the game ({other})"
    for other in CUP_OTHER_COMPETITIONS:
        if other in lowered:
            return f"another competition ({other})"
    side = side_competition(raw, candidate.get("channel"))
    if side:
        return f"another competition with the same names ({side})"
    phrase = not_a_highlight(raw)
    if phrase:
        return f"not a highlight ({phrase})"
    duration = candidate["duration"]
    if duration is not None and duration < 60:
        return "too short to be a match highlight"
    return None


def cup_fixtures(cup_keys: list[str], args) -> list[dict]:
    """One card per cup tie, built from the competition's own YouTube uploads."""
    fixtures: list[dict] = []
    for key in cup_keys:
        cup = CUPS[key]
        try:
            candidates = yt_search(cup["query"], args.cup_search, args.timeout, args.cup_window)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            print(f"warning: cup lookup failed for {cup['query']} ({error})", file=sys.stderr)
            continue
        if args.debug:
            print(f"debug: cup {key} -> {len(candidates)} results", file=sys.stderr)
        if not candidates:
            gha_warning(f"cup {key}: the YouTube search for {cup['query']!r} returned nothing")
        for candidate in candidates:
            reason = cup_reject(candidate, key)
            home = away = None
            if reason is None:
                home, away = cup_pair(candidate["titleRaw"])
                if not home:
                    reason = "no two clubs in the title"
            if reason:
                if args.debug:
                    print(
                        f"debug: cup {key}: dropping {candidate['id']} ({reason}) "
                        f":: {candidate['titleRaw']}",
                        file=sys.stderr,
                    )
                continue
            try:
                uploaded = cup_upload_date(
                    candidate["id"], candidate.get("channelId"), args.timeout
                )
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                print(f"warning: no upload date for {candidate['id']} ({error})", file=sys.stderr)
                uploaded = None
            fixture = _find_cup_fixture(fixtures, home, away, uploaded)
            if fixture is None:
                fixture = {
                    "id": f"cup-{key}-{len(fixtures)}",
                    "cup": key,
                    "area": None,
                    "league": cup["name"],
                    "date": uploaded,
                    "kickoff": None,
                    "home": home,
                    "away": away,
                    "homeZh": None,
                    "awayZh": None,
                    "titleZh": None,
                    "videoId": None,
                    "nowtvUrl": None,
                    "durationSeconds": None,
                    "publishedAt": None,
                    "youtube": [],
                    "previews": [],
                    "upcoming": False,
                }
                fixtures.append(fixture)
            # the earliest upload is the one closest to kick-off
            if uploaded:
                fixture["date"] = min(fixture["date"], uploaded) if fixture["date"] else uploaded
            fixture["youtube"].append(candidate)

    for fixture in fixtures:
        fixture["youtube"].sort(key=lambda item: _yt_score(item, fixture), reverse=True)
        fixture["youtube"] = [
            {
                "id": candidate["id"],
                "title": candidate["title"],
                "titleHidden": (
                    looks_like_score(candidate["titleRaw"])
                    or bool(spoiler_hits(candidate["titleRaw"]))
                    or len(
                        editorial_words(candidate["titleRaw"], fixture["home"], fixture["away"])
                    ) >= 3
                ),
                "channel": candidate["channel"],
                "durationSeconds": candidate["duration"],
            }
            for candidate in fixture["youtube"][: args.yt_results]
        ]
        for entry in fixture["youtube"]:
            if entry["titleHidden"]:
                entry["title"] = None
    # keep YouTube's own order while trimming, so an unknown upload date cannot
    # push a whole competition out of the list
    per_cup: dict[str, int] = {}
    limited = []
    for fixture in fixtures:
        used = per_cup.get(fixture["cup"], 0)
        if used >= args.cup_results:
            continue
        per_cup[fixture["cup"]] = used + 1
        limited.append(fixture)
    # for display: dated cards newest first, then whatever had no date
    limited.sort(key=lambda fixture: (fixture["date"] or "", bool(fixture["date"])), reverse=True)
    for key in cup_keys:
        if not any(fixture["cup"] == key for fixture in limited):
            print(f"warning: no fixtures found for the {key}", file=sys.stderr)
            gha_warning(f"cup {key}: no fixtures survived")
    return limited


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build(args) -> dict:
    fixtures = nowtv_fixtures(args.areas, args.page_size, args.timeout)
    cups = [] if args.no_youtube else cup_fixtures(args.cups, args)
    if args.limit:
        fixtures = fixtures[: args.limit]
    for fixture in fixtures:
        fixture["previews"] = []
        fixture["upcoming"] = False
    for index, fixture in enumerate(fixtures):
        fixture["id"] = fixture.get("id") or f"nowtv-{fixture['videoId']}-{index}"

    if not args.no_upcoming:
        try:
            scheduled = schedule_fixtures(args.areas, args.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"warning: schedule unavailable ({error})", file=sys.stderr)
            scheduled = []
        if scheduled:
            added = merge_schedule(fixtures, scheduled, args.upcoming_days)
            if added and args.debug:
                print(f"debug: added {added} fixtures from the schedule", file=sys.stderr)
            for index, fixture in enumerate(fixtures):
                if not fixture.get("id"):
                    fixture["id"] = f"schedule-{index}"

    if cups:
        fixtures.extend(cups)

    # the week ahead first, in kick-off order; then what has been played, newest
    # first, keeping the order nowtv_fixtures already sorted
    for fixture in fixtures:
        # the list is a day or two stale by the time it is rendered, so the flag
        # is decided here rather than when the fixture was written
        fixture["upcoming"] = is_upcoming(fixture)
    ahead = [fixture for fixture in fixtures if fixture["upcoming"]]
    played = [fixture for fixture in fixtures if not fixture["upcoming"]]
    ahead.sort(key=lambda fixture: (fixture.get("kickoff") or fixture.get("date") or "", fixture["home"]))
    # a stable sort keeps the order NOW TV handed over for the league matches
    played.sort(key=lambda fixture: fixture.get("date") or "", reverse=True)
    fixtures = ahead + played

    if not args.no_youtube and fixtures:
        for fixture in fixtures:
            # cup cards already carry the videos the fixture was read from
            if fixture.get("cup"):
                continue
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
        fields += [clip.get("title") for clip in match.get("previews") or []]
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
    parser.add_argument(
        "--cups",
        default=",".join(DEFAULT_CUPS),
        help=f"cup competitions listed from YouTube (default: {','.join(DEFAULT_CUPS)}, '' to skip)",
    )
    parser.add_argument("--cup-results", type=int, default=4, help="max matches kept per cup")
    parser.add_argument("--cup-search", type=int, default=20, help="YouTube results examined per cup")
    parser.add_argument(
        "--cup-window",
        choices=["all", *YT_WINDOWS],
        default="year",
        help="upload-date filter for cup uploads (default: year)",
    )
    parser.add_argument("--yt-search", type=int, default=8, help="YouTube results examined")
    parser.add_argument("--yt-results", type=int, default=3, help="YouTube links kept")
    parser.add_argument(
        "--upcoming-days",
        type=int,
        default=7,
        help="how many days of fixtures to list ahead (default: 7)",
    )
    parser.add_argument(
        "--no-upcoming", action="store_true", help="do not list fixtures that have not kicked off"
    )
    parser.add_argument("--preview-results", type=int, default=3, help="preview links kept")
    parser.add_argument("--preview-search", type=int, default=12, help="preview results examined")
    parser.add_argument(
        "--preview-window",
        choices=[*YT_WINDOWS],
        default="week",
        help="upload-date filter for previews (default: week)",
    )
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
    cups = [cup.strip() for cup in args.cups.split(",") if cup.strip() in CUPS]
    if args.cups.strip() and not cups:
        print(
            f"warning: --cups must contain one of {', '.join(CUPS)}; skipping the cups",
            file=sys.stderr,
        )
    args.cups = cups

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
