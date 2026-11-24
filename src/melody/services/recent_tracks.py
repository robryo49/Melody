"""Which tracks the Recent page shows: the N most recently added, newest first.

N is set in the Recent page's header (see ``widgets.recent_count``).

It is an automatic playlist's rules (see ``automatic_playlists``) built from
the setting, so "added" means the same thing here as there: when Melody first
saw the track.
"""

from melody.services.automatic_playlists import evaluate_rules


def recent_rules(count):
    """The rules that pick the ``count`` most recently added tracks, newest
    first."""
    return {
        "match": "all",
        "conditions": [],
        "sort": {"key": "added", "descending": True},
        "limit": max(1, int(count)),
    }


def select_recent_tracks(tracks, settings, now=None):
    """The recent tracks among ``tracks``, newest first."""
    return evaluate_rules(recent_rules(settings.recent_count), tracks, now)
