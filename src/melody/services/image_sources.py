"""Where the image picker gets its images.

The picker (``widgets/image_picker.py``) offers up to three ways to choose an
image:

* a file on this computer (always);
* a one-click shortcut, for a track: the cover of its album, if the library
  already has one (``ImageSearch.shortcut``);
* a search on YouTube, whose results are thumbnails to pick from.

What is searched for depends on what the image is for, so each kind has a
function that builds the ``ImageSearch`` the picker runs: ``track_search``,
``album_search``, ``artist_search`` and ``playlist_search``. The searching
itself is in ``youtube_images.py``.

Everything that fetches something blocks: the picker runs it in threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from melody.features import YOUTUBE_IMAGE_SEARCH_ENABLED
from melody.services import artwork


class ImageSearchError(Exception):
    """A search failed; the message is meant to be shown as it is."""


@dataclass
class ImageSource:
    """One image that can be had in a single step (see
    ``library_album_source``). ``loader()`` returns the full-size image bytes,
    or ``None`` when there is nothing."""

    name: str
    loader: Callable[[], bytes | None]
    detail: str = ""


@dataclass
class ImageResult:
    """One result of a search: a picture to show, and the picture to use.

    ``load_thumbnail()`` returns an image to show in the list of results (any
    shape: the picker crops it to a square). ``load_full()`` returns the
    image to use once the person has picked it, as big as it can be. Both
    return ``None`` (or raise) when the picture cannot be had; the picker then
    leaves the result out.
    """

    title: str
    load_thumbnail: Callable[[], bytes | None]
    load_full: Callable[[], bytes | None]
    detail: str = ""


@dataclass
class ImageSearch:
    """What the picker searches for.

    ``query`` is what the search bar starts with: when it is not empty the
    search is run as soon as the picker opens. ``search(query)`` returns the
    results, or raises ``ImageSearchError``; it is ``None`` when this build has
    no search (see ``melody.features``), and the picker then only offers the
    file chooser and the shortcut.

    ``hint`` says in a few words what the search looks for. ``suggestions``
    are images fetched from other sites (Deezer, iTunes...), shown as tiles
    above the search. ``shortcut`` is a one-click image (shown as a button,
    labelled ``shortcut_label``), and ``dedupe`` hides results whose pictures
    look the same (the songs of one album all show its cover).
    """

    query: str = ""
    search: Callable[[str], list[ImageResult]] | None = None
    hint: str = ""
    suggestions: list[ImageSource] = field(default_factory=list)
    shortcut: ImageSource | None = None
    shortcut_label: str = "Use album cover"
    dedupe: bool = False


def _is_unknown(value: str) -> bool:
    return artwork._normalize_text(value or "") in (
        "",
        "unknown artist",
        "unknown album",
        "various artists",
    )


def _words(*parts: str) -> str:
    """The non-empty ``parts`` joined with spaces."""
    return " ".join(
        part.strip() for part in parts if part and part.strip()
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
    never has anything to offer, and the picker shows a placeholder.
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


def track_sources(artist: str, title: str) -> list[ImageSource]:
    """Covers found by searching for the song itself: Deezer and iTunes.

    They work for a single too, which has no album to search for. The cover
    is that of the release the song is on, so it is often the album's.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()

    return [
        ImageSource(
            "Deezer (song)",
            _unless_unknown(
                lambda: artwork.get_deezer_track_image(artist, title),
                artist, title,
            ),
        ),
        ImageSource(
            "iTunes (song)",
            _unless_unknown(
                lambda: artwork.get_itunes_track_image(artist, title),
                artist, title,
            ),
        ),
    ]


def _youtube(name: str):
    """The function ``name`` of ``youtube_images``, or ``None`` when this
    build leaves the YouTube search out."""
    if not YOUTUBE_IMAGE_SEARCH_ENABLED:
        return None

    try:
        from melody.services import youtube_images
    except ModuleNotFoundError as error:
        if error.name != "melody.services.youtube_images":
            raise

        return None

    return getattr(youtube_images, name)


def track_search(
        title: str,
        artist: str,
        album: str = "",
        library_tracks=(),
) -> ImageSearch:
    """Images for a track: the thumbnails of its music video on YouTube,
    searched as "title artist"; suggestions from other sites, both for its
    album (when it has one) and for the song itself (so a single has some
    too); and the cover of its album.

    The values are those in the edit window, not the track's own: they may
    have been changed there without being saved yet. The album artist is
    always the artist typed in the window. A single has no album, so there is
    no album cover to borrow.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()

    shortcut = (
        library_album_source(list(library_tracks), artist, album)
        if album and not _is_unknown(album)
        else None
    )

    return ImageSearch(
        query=_words(title, "" if _is_unknown(artist) else artist),
        search=_youtube("search_music_videos"),
        hint="Music videos on YouTube",
        suggestions=(
            album_sources(artist, album)
            if album and not _is_unknown(album)
            else []
        ) + track_sources(artist, title),
        shortcut=shortcut,
    )


def album_search(artist: str, album: str) -> ImageSearch:
    """Images for an album: covers found on YouTube Music, searched as
    "artist album"."""
    artist = (artist or "").strip()
    album = (album or "").strip()

    return ImageSearch(
        query=(
            ""
            if _is_unknown(album)
            else _words("" if _is_unknown(artist) else artist, album)
        ),
        search=_youtube("search_album_covers"),
        hint="Album covers on YouTube Music",
        suggestions=album_sources(artist, album),
        dedupe=True,
    )


def artist_search(name: str) -> ImageSearch:
    """Images for an artist: the pictures of the YouTube channels found for
    their name."""
    name = (name or "").strip()

    return ImageSearch(
        query="" if _is_unknown(name) else name,
        search=_youtube("search_artist_pictures"),
        hint="YouTube channel pictures",
        suggestions=artist_sources(name),
    )


def playlist_search() -> ImageSearch:
    """Images for a playlist: the thumbnails of whatever videos the person
    searches for (a playlist's name says nothing about what it should look
    like, so nothing is searched until they ask)."""
    return ImageSearch(
        query="",
        search=_youtube("search_videos"),
        hint="Videos on YouTube",
    )


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
