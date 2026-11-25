"""Playlist categories: a name a playlist can be filed under.

A playlist with no category stays in the Playlists page. One with a category
is listed on a page of its own under Playlists in the sidebar, one per
category, so playlists that belong together (the ones for movies, say) are
kept apart from the rest.

A category is only a name kept on its playlists: it exists while at least one
playlist has it. Names are compared without regard to case ("movies" is
"Movies"), and the first spelling in use is the one kept.

Everything here is GTK-free.
"""

from __future__ import annotations


def clean_category(value):
    """``value`` as it is stored: stripped, with runs of spaces made one, and
    None when nothing is left (no category)."""
    text = " ".join(str(value or "").split())
    return text or None


def category_key(value):
    """What two spellings of a category are compared by (None for none)."""
    cleaned = clean_category(value)
    return cleaned.casefold() if cleaned else None


def same_category(first, second):
    """True when ``first`` and ``second`` are the same category, whatever the
    case (and True for two playlists that have none)."""
    return category_key(first) == category_key(second)


def category_names(playlists):
    """The categories in use among ``playlists`` (dicts with a ``category``),
    A-Z, each spelled as the first playlist that has it spells it."""
    names = {}

    for playlist in playlists:
        name = clean_category(playlist.get("category"))

        if name:
            names.setdefault(name.casefold(), name)

    return sorted(names.values(), key=str.casefold)


def category_suggestions(names):
    """``names`` (category names) as the suggestions of a ``SuggestionField``."""
    return [
        {"value": name, "group": name.casefold()}
        for name in names
    ]
