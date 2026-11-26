"""Automatic playlists: playlists defined by rules instead of a list of tracks.

An automatic playlist has no tracks of its own. Its contents are worked out
from the library each time they are needed, so a track joins or leaves it by
itself as it is played, marked as a favorite, added, edited...

The rules are a plain dictionary (kept as JSON in the database)::

    {
        "match": "all",             # "all" conditions, or "any" of them
        "conditions": [
            {"field": "play_count", "op": "at_least", "value": 5},
            {"field": "favorite", "op": "is", "value": True},
        ],
        "sort": {"key": "play_count", "descending": True},
        "limit": 50,                # at most this many tracks, or None
    }

Everything here is GTK-free.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from melody.services.genres import split_genres
from melody.services.track_range import track_length

# field: (name shown to the person, kind of value). The kind decides which
# operators the field offers.
FIELDS = {
    "title": ("Title", "text"),
    "artist": ("Artist", "text"),
    "album": ("Album", "text"),
    "genre": ("Genre", "text"),
    "year": ("Year", "number"),
    "play_count": ("Play count", "number"),
    "rating": ("Rating (stars)", "number"),
    "duration": ("Duration (minutes)", "number"),
    "added": ("Date added", "date"),
    "favorite": ("Favorite", "flag"),
}

# kind: {operator: name shown to the person}
OPERATORS = {
    "text": {
        "contains": "contains",
        "not_contains": "does not contain",
        "is": "is",
        "is_not": "is not",
        "starts_with": "starts with",
    },
    "number": {
        "is": "is",
        "is_not": "is not",
        "greater": "is greater than",
        "less": "is less than",
        "at_least": "is at least",
        "at_most": "is at most",
    },
    # The value is a number of days, counted back from now.
    "date": {
        "in_last_days": "is in the last",
        "not_in_last_days": "is not in the last",
    },
    "flag": {
        "is": "is",
    },
}

# What a date's value counts, and what a flag's value is called.
DATE_UNIT = "days"
FLAG_NAMES = {True: "yes", False: "no"}

# What a playlist can be sorted by: key -> name shown to the person.
SORT_KEYS = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "year": "Year",
    "play_count": "Play count",
    "rating": "Rating",
    "duration": "Duration",
    "added": "Date added",
}


def default_rules():
    """The rules of a new automatic playlist: every track, by title."""
    return {
        "match": "all",
        "conditions": [],
        "sort": {"key": "title", "descending": False},
        "limit": None,
    }


# Ready-made automatic playlists, to start from.
PRESETS = [
    {
        "name": "Most played",
        "description": "The 50 tracks you played the most",
        "rules": {
            "match": "all",
            "conditions": [
                {"field": "play_count", "op": "at_least", "value": 1},
            ],
            "sort": {"key": "play_count", "descending": True},
            "limit": 50,
        },
    },
    {
        "name": "Never played",
        "description": "Tracks you have not listened to yet",
        "rules": {
            "match": "all",
            "conditions": [
                {"field": "play_count", "op": "is", "value": 0},
            ],
            "sort": {"key": "added", "descending": True},
            "limit": None,
        },
    },
    {
        "name": "Recently added",
        "description": "What was added to the library in the last 30 days",
        "rules": {
            "match": "all",
            "conditions": [
                {"field": "added", "op": "in_last_days", "value": 30},
            ],
            "sort": {"key": "added", "descending": True},
            "limit": None,
        },
    },
    {
        "name": "Favorite and often played",
        "description": "Favorites you played at least 5 times",
        "rules": {
            "match": "all",
            "conditions": [
                {"field": "favorite", "op": "is", "value": True},
                {"field": "play_count", "op": "at_least", "value": 5},
            ],
            "sort": {"key": "play_count", "descending": True},
            "limit": None,
        },
    },
]


# ----------------------------------------------------------------------
# Checking and storing rules
# ----------------------------------------------------------------------

def _clean_value(field, kind, value):
    """``value`` as the field needs it, or ``ValueError`` saying why not."""
    name = FIELDS[field][0]

    if kind == "flag":
        if isinstance(value, bool):
            return value

        raise ValueError(f"{name} needs yes or no")

    if kind == "text":
        return str(value if value is not None else "").strip()

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} needs a number") from None

    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} needs a number")

    if kind == "date" and number < 0:
        raise ValueError(f"{name} needs a number of days that is not negative")

    return int(number) if number == int(number) else number


def normalize_rules(rules):
    """A checked copy of ``rules``, with every missing part filled in.

    Raises ``ValueError`` (with a message fit to show) if they make no sense.
    """
    if not isinstance(rules, dict):
        raise ValueError("The rules must be a dictionary")

    match = rules.get("match", "all")

    if match not in ("all", "any"):
        raise ValueError("The rules must match \"all\" or \"any\" conditions")

    conditions = []

    for raw in rules.get("conditions") or []:
        if not isinstance(raw, dict):
            raise ValueError("A condition must be a dictionary")

        field = raw.get("field")

        if field not in FIELDS:
            raise ValueError(f"Unknown field: {field!r}")

        kind = FIELDS[field][1]
        operator = raw.get("op")

        if operator not in OPERATORS[kind]:
            raise ValueError(
                f"{FIELDS[field][0]} cannot be compared with {operator!r}"
            )

        conditions.append({
            "field": field,
            "op": operator,
            "value": _clean_value(field, kind, raw.get("value")),
        })

    sort = rules.get("sort") or {}
    key = sort.get("key", "title")

    if key not in SORT_KEYS:
        raise ValueError(f"Cannot sort by {key!r}")

    limit = rules.get("limit")

    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("The limit must be a number") from None

        if limit < 1:
            raise ValueError("The limit must be at least 1")

    return {
        "match": match,
        "conditions": conditions,
        "sort": {"key": key, "descending": bool(sort.get("descending"))},
        "limit": limit,
    }


def dumps_rules(rules):
    """The rules as the JSON text kept in the database (checked first)."""
    return json.dumps(
        normalize_rules(rules),
        separators=(",", ":"),
        sort_keys=True,
    )


def loads_rules(text):
    """The rules kept as JSON text. Text that is damaged or makes no sense
    gives the default rules rather than an error, so one bad row never stops
    the page from showing."""
    try:
        return normalize_rules(json.loads(text))
    except (TypeError, ValueError):
        return default_rules()


# ----------------------------------------------------------------------
# Working out which tracks match
# ----------------------------------------------------------------------

def _normalize_text(value):
    """Lower case without accents, so "beyonce" finds "Beyoncé"."""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.casefold().strip()


def _parse_added(value):
    """When a track was added (a UTC ``YYYY-MM-DD HH:MM:SS`` text from the
    database), or None if it is missing or unreadable."""
    if not value:
        return None

    try:
        moment = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment


def _year_of(track):
    found = re.search(r"\d{4}", str(track.get("year") or ""))
    return int(found.group()) if found else None


def _field_value(track, field):
    if field == "year":
        return _year_of(track)

    if field == "play_count":
        return int(track.get("play_count") or 0)

    if field == "rating":
        # Ratings are kept in half stars: a condition counts whole stars.
        return int(track.get("rating") or 0) / 2

    if field == "duration":
        return track_length(track) / 60

    if field == "added":
        return _parse_added(track.get("added_at"))

    if field == "favorite":
        return bool(track.get("favorite"))

    return str(track.get(field) or "")


def _genre_matches(track, operator, expected):
    """A track's genres are compared one by one, so "is Rock" finds a track
    with Rock among several genres."""
    genres = [
        _normalize_text(name)
        for name in split_genres(track.get("genre"))
    ]
    wanted = _normalize_text(expected)

    if operator == "contains":
        return any(wanted in name for name in genres)
    if operator == "not_contains":
        return not any(wanted in name for name in genres)
    if operator == "is":
        return wanted in genres
    if operator == "is_not":
        return wanted not in genres

    return any(name.startswith(wanted) for name in genres)


def _matches(track, condition, now):
    field = condition["field"]
    operator = condition["op"]
    expected = condition["value"]
    if field == "genre":
        return _genre_matches(track, operator, expected)

    kind = FIELDS[field][1]
    actual = _field_value(track, field)

    if kind == "text":
        actual = _normalize_text(actual)
        expected = _normalize_text(expected)

        if operator == "contains":
            return expected in actual
        if operator == "not_contains":
            return expected not in actual
        if operator == "is":
            return actual == expected
        if operator == "is_not":
            return actual != expected

        return actual.startswith(expected)

    if kind == "flag":
        return actual == expected

    if kind == "date":
        if actual is None:
            # No date known: not in the last N days, whatever N is.
            return operator == "not_in_last_days"

        recent = actual >= now - timedelta(days=expected)
        return recent if operator == "in_last_days" else not recent

    if actual is None:
        # A number the track does not have (no year, say) is not equal to
        # anything, and cannot be compared.
        return operator == "is_not"

    if operator == "is":
        return actual == expected
    if operator == "is_not":
        return actual != expected
    if operator == "greater":
        return actual > expected
    if operator == "less":
        return actual < expected
    if operator == "at_least":
        return actual >= expected

    return actual <= expected


def _sort_value(track, key):
    if key == "year":
        return _year_of(track) or 0

    if key == "play_count":
        return int(track.get("play_count") or 0)

    if key == "rating":
        return int(track.get("rating") or 0)

    if key == "duration":
        return track_length(track)

    if key == "added":
        moment = _parse_added(track.get("added_at"))
        return moment.timestamp() if moment is not None else 0.0

    return _normalize_text(track.get(key) or "")


def evaluate_rules(rules, tracks, now=None):
    """The tracks of ``tracks`` that ``rules`` select, sorted and cut to the
    limit, as the playlist would list them.

    ``now`` is the moment "in the last N days" counts back from (default: now).
    """
    rules = normalize_rules(rules)
    now = now or datetime.now(timezone.utc)

    conditions = rules["conditions"]
    combine = all if rules["match"] == "all" else any

    if conditions:
        chosen = [
            track for track in tracks
            if combine(_matches(track, condition, now) for condition in conditions)
        ]
    else:
        chosen = list(tracks)

    sort = rules["sort"]

    # Ties keep title order, so the same rules always give the same list.
    chosen.sort(key=lambda track: _normalize_text(track.get("title") or ""))
    chosen.sort(
        key=lambda track: _sort_value(track, sort["key"]),
        reverse=sort["descending"],
    )

    if rules["limit"] is not None:
        chosen = chosen[:rules["limit"]]

    return chosen


# ----------------------------------------------------------------------
# Saying what the rules are
# ----------------------------------------------------------------------

def describe_condition(condition):
    """One condition in words, like ``Play count is at least 5``."""
    field = condition["field"]
    name, kind = FIELDS[field]
    operator = OPERATORS[kind][condition["op"]]
    value = condition["value"]

    if kind == "flag":
        value = FLAG_NAMES[value]
    elif kind == "date":
        value = f"{value} {DATE_UNIT}"
    elif kind == "text":
        value = f"\u201c{value}\u201d"

    return f"{name} {operator} {value}"


def describe_rules(rules):
    """The rules in words, for a subtitle: the conditions, then the limit."""
    rules = normalize_rules(rules)

    if rules["conditions"]:
        joiner = " and " if rules["match"] == "all" else " or "
        text = joiner.join(
            describe_condition(condition) for condition in rules["conditions"]
        )
    else:
        text = "Every track"

    if rules["limit"] is not None:
        text += f" \u2022 at most {rules['limit']} tracks"

    return text
