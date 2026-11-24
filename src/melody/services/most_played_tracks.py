"""Which tracks the Most played page shows: the N most played, the most played
first.

N is set in the Most played page's header. A track that was never played is
not "played" at all, so it is left out, even when that leaves fewer than N.

It is an automatic playlist's rules (see ``automatic_playlists``) built from
the setting, so play counts mean the same thing here as there.
"""

from melody.services.automatic_playlists import evaluate_rules


def most_played_rules(count):
    """The rules that pick the ``count`` most played tracks."""
    return {
        "match": "all",
        "conditions": [
            {"field": "play_count", "op": "at_least", "value": 1},
        ],
        "sort": {"key": "play_count", "descending": True},
        "limit": max(1, int(count)),
    }


def select_most_played_tracks(tracks, settings, now=None):
    """The most played tracks among ``tracks``, the most played first."""
    return evaluate_rules(
        most_played_rules(settings.most_played_count),
        tracks,
        now,
    )
