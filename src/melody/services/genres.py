"""Genres: how several of them are kept together, and the list of them in a
library.

A track's genres are kept as one text, separated by ``GENRE_SEPARATOR``: the
``genre`` column of the database, and what the edit window shows. In the file
each genre is a tag of its own (see ``services.metadata``).

GTK-free.
"""

import re
from collections import Counter

GENRE_SEPARATOR = ", "

# A file may separate its genres with commas or semicolons.
_SEPARATORS = re.compile(r"[,;]")


def split_genres(text) -> list[str]:
    """The genres in ``text``, in order, without empty ones or repeats (a repeat
    is one that differs only in case)."""
    if not text:
        return []

    genres = []
    seen = set()

    for part in _SEPARATORS.split(str(text)):
        name = part.strip()
        key = name.casefold()

        if name and key not in seen:
            seen.add(key)
            genres.append(name)

    return genres


def join_genres(names) -> str:
    """The text that holds ``names`` (see the module's description)."""
    return GENRE_SEPARATOR.join(names)


def same_genres(first, second) -> bool:
    """True when the two texts name the same genres, in any order and any
    case."""
    return (
        {name.casefold() for name in split_genres(first)}
        == {name.casefold() for name in split_genres(second)}
    )


def genre_names(tracks) -> list[str]:
    """Every distinct genre of ``tracks``, A-Z, each spelled the way most of
    the tracks spell it."""
    spellings = {}

    for track in tracks:
        for name in split_genres(track.get("genre")):
            spellings.setdefault(name.casefold(), Counter())[name] += 1

    return [
        spellings[key].most_common(1)[0][0]
        for key in sorted(spellings)
    ]
