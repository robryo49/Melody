"""Which tracks the Recent page shows: the ones added in the last N days, or
the N most recently added, as the settings say.

It is an automatic playlist's rules (see ``automatic_playlists``) built from
the settings, so "added" means the same thing here as there: when Melody first
saw the track.
"""

from melody.services.automatic_playlists import evaluate_rules


def recent_rules(by_days, days, count):
    """The rules that pick the recent tracks, newest first."""
    sort = {"key": "added", "descending": True}

    if by_days:
        return {
            "match": "all",
            "conditions": [
                {"field": "added", "op": "in_last_days", "value": days},
            ],
            "sort": sort,
            "limit": None,
        }

    return {
        "match": "all",
        "conditions": [],
        "sort": sort,
        "limit": max(1, int(count)),
    }


def select_recent_tracks(tracks, settings, now=None):
    """The recent tracks among ``tracks``, newest first."""
    rules = recent_rules(
        settings.recent_by_days,
        settings.recent_days,
        settings.recent_count,
    )
    return evaluate_rules(rules, tracks, now)
