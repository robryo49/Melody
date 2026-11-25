"""Finding what is already in the library, before importing it again.

Importing the same song twice is not refused: the second copy just gets a
number in its name ("Title (2).mp3"), and the library ends up with the song
twice. ``find_duplicates`` spots those cases beforehand, so the person can be
asked.

Two tracks are taken to be the same song when they have the same title and the
same artist, and (when both lengths are known) last about the same. The album
does not matter: a song is the same song on a single and on an album. The
length keeps a live version or a remix from being taken for the studio one.

It is only a warning. A wrong guess costs the person a click ("Import
anyway"), so it leans towards speaking up.

Nothing here touches the library or any file.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from melody.services.track_import import PendingTrack

# Two files of the same song never last exactly the same (an encoder adds a
# little silence, an upload cuts the intro). Up to this many seconds apart
# they are still the same song; further apart, it is another version.
DURATION_TOLERANCE = 5.0


@dataclass
class Duplicate:
    """A file about to be imported that looks like a track already known.

    ``existing`` is the library's track it looks like, or, when there is none
    (it only repeats another file of the same import), ``earlier`` is that
    other file.
    """

    item: PendingTrack
    existing: dict | None = None
    earlier: PendingTrack | None = None


def _normalize(text) -> str:
    """``text`` without what makes no difference to a person: capitals,
    spacing, punctuation ("AC/DC" is "ACDC", "Don't" is "Dont")."""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    squeezed = re.sub(r"[\W_]+", "", text)

    # A name made of nothing but symbols keeps them, or all such names would
    # be the same.
    return squeezed or text


def _key(title, artist):
    """What identifies a song: its title and its artist. None when there is
    no title, since nothing can be said about such a track."""
    title = _normalize(title)

    if not title:
        return None

    return (title, _normalize(artist))


def _seconds(value) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _same_length(first, second) -> bool:
    """Whether two lengths (in seconds) can be the same song's. When one is
    not known there is nothing to tell them apart by: they can."""
    first, second = _seconds(first), _seconds(second)

    if not first or not second:
        return True

    return abs(first - second) <= DURATION_TOLERANCE


def find_duplicates(pending, library_tracks) -> list[Duplicate]:
    """The files in ``pending`` that look like a track of ``library_tracks``
    (as the library describes them), or like a file earlier in ``pending``.

    The first of several equal files is not a duplicate, only the ones after
    it. A file is always judged by what it will be called once imported (the
    title and artist as edited), not by what its tags said.
    """
    known: dict[tuple, list[dict]] = {}

    for track in library_tracks:
        key = _key(
            track.get("title"),
            track.get("artist") or track.get("album_artist"),
        )

        if key is not None:
            known.setdefault(key, []).append(track)

    seen: dict[tuple, list[PendingTrack]] = {}
    duplicates = []

    for item in pending:
        key = _key(item.title, item.artist)

        if key is None:
            continue

        length = item.metadata.duration

        existing = next(
            (
                track for track in known.get(key, ())
                if _same_length(length, track.get("duration"))
            ),
            None,
        )

        if existing is not None:
            duplicates.append(Duplicate(item, existing=existing))
        else:
            earlier = next(
                (
                    other for other in seen.get(key, ())
                    if _same_length(length, other.metadata.duration)
                ),
                None,
            )

            if earlier is not None:
                duplicates.append(Duplicate(item, earlier=earlier))

        seen.setdefault(key, []).append(item)

    return duplicates
