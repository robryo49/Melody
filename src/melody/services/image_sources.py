"""Where the image picker looks for suggestions.

Each ``ImageSource`` is one place an image can come from. ``loader()`` does
the (slow, network) work and returns the full-size image bytes, or ``None``
when that source has nothing; it is always run in a background thread.

The lists below always have the same number of sources for a given kind of
image: a source with nothing to offer just returns ``None``, and the picker
shows a placeholder for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from melody.services import artwork


@dataclass
class ImageSource:
    name: str
    loader: Callable[[], bytes | None]
    detail: str = ""


def _is_unknown(value: str) -> bool:
    return artwork._normalize_text(value or "") in (
        "",
        "unknown artist",
        "unknown album",
        "various artists",
    )


def _unless_unknown(loader, *names):
    """``loader``, but returning ``None`` when a name is empty or unknown."""
    def guarded():
        if any(_is_unknown(name) for name in names):
            return None
        return loader()

    return guarded


def artist_sources(artist: str) -> list[ImageSource]:
    """Artist portraits: Deezer, Wikimedia Commons and Fanart.tv.

    Fanart.tv needs an API key (``MELODY_FANART_API_KEY``); without one it
    never has anything to offer.
    """
    artist = (artist or "").strip()

    return [
        ImageSource(
            "Deezer",
            _unless_unknown(
                lambda: artwork.get_deezer_artist_image(artist), artist,
            ),
        ),
        ImageSource(
            "Wikimedia",
            _unless_unknown(
                lambda: artwork.get_wikimedia_artist_image(artist), artist,
            ),
        ),
        ImageSource(
            "Fanart.tv",
            _unless_unknown(
                lambda: artwork.get_fanart_artist_image(artist), artist,
            ),
        ),
    ]


def album_sources(artist: str, album: str) -> list[ImageSource]:
    """Album covers: Deezer, iTunes and the MusicBrainz Cover Art Archive."""
    artist = (artist or "").strip()
    album = (album or "").strip()

    return [
        ImageSource(
            "Deezer",
            _unless_unknown(
                lambda: artwork.get_deezer_album_image(artist, album),
                artist, album,
            ),
        ),
        ImageSource(
            "iTunes",
            _unless_unknown(
                lambda: artwork.get_itunes_album_image(artist, album),
                artist, album,
            ),
        ),
        ImageSource(
            "Cover Art Archive",
            _unless_unknown(
                lambda: artwork.get_online_album_artwork(artist, album),
                artist, album,
            ),
        ),
    ]


def library_album_source(
        tracks: list[dict],
        album_artist: str,
        album: str,
) -> ImageSource:
    """The cover of an album that already exists in the library.

    ``tracks`` is the library; the album is found by its album artist and
    title. If there is no such album, or it has no cover, the source simply
    has nothing to offer.
    """
    def key(value):
        return str(value or "").strip().casefold()

    wanted = (key(album_artist), key(album))

    matching = [
        track for track in tracks
        if (
                key(track.get("album_artist") or track.get("artist")),
                key(track.get("album")),
        ) == wanted
    ]

    def load():
        # The album's original, full-size cover first: the image in its
        # folder, or else one embedded in a file...
        for track in matching:
            try:
                data = artwork.get_album_artwork_bytes(Path(track["path"]))
            except Exception:
                data = None

            if data:
                return data

        # ...then the 256px copy Melody already generated for the grid.
        for track in matching:
            cached = track.get("album_artwork_path")

            if cached:
                try:
                    return Path(cached).read_bytes()
                except OSError:
                    continue

        return None

    title = str(album or "").strip()
    return ImageSource(
        "Album image",
        load,
        f"“{title}”" if title else "",
    )
