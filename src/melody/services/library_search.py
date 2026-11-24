"""Searching the whole library at once: artists, albums, tracks, compilations.

Everything here is GTK-free. A search is case- and accent-insensitive
("beyonce" finds "Beyoncé") and finds what *contains* the words typed. Results
whose name starts with the words come first, then the others, A-Z.
"""

import unicodedata

from melody.services.compilations import is_compilation
from melody.services.settings import get_settings
from melody.services.singles import has_album


def normalize(text):
    """Lower case without accents."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.casefold().strip()


def artist_name(track):
    """The artist a track is filed under: its album artist, as the Artists
    page does."""
    return str(
        track.get("album_artist") or track.get("artist") or "Unknown Artist"
    )


def _rank(name, needle):
    """Sort key: names starting with the search first, then names containing
    it, each group A-Z."""
    name = normalize(name)
    return (0 if name.startswith(needle) else 1, name)


def search_library(tracks, query, settings=None):
    """What ``query`` finds among ``tracks``.

    Returns ``{"artists": [...], "albums": [...], "tracks": [...],
    "compilations": [...]}``, each in order of relevance, never cut. An empty
    query finds nothing.

    * an artist is ``{"name", "album_count", "track_count", "artwork_path",
      "track"}``;
    * an album is ``{"title", "artist", "track_count", "artwork_path",
      "track"}``, where ``track`` is one of its tracks (what the pages use to
      find the album again);
    * tracks and compilations are the library's own track dictionaries.

    Albums are the Albums page's: a compilation is only part of its album
    when the setting says so. An album is found by its title or its artist's
    name.
    """
    results = {"artists": [], "albums": [], "tracks": [], "compilations": []}
    needle = normalize(query)

    if not needle:
        return results

    settings = settings or get_settings()
    compilations_in_albums = settings.show_compilations_in_albums

    artists = {}
    albums = {}

    for track in tracks:
        compilation = is_compilation(track)
        artist = artist_name(track)
        artist_key = artist.casefold()
        album = str(track.get("album") or "").strip()

        entry = artists.setdefault(
            artist_key,
            {
                "name": artist,
                "albums": set(),
                "track_count": 0,
                "artwork_path": None,
                "track": track,
            },
        )
        entry["track_count"] += 1

        if album:
            entry["albums"].add(album.casefold())

        if not entry["artwork_path"]:
            entry["artwork_path"] = track.get("artist_artwork_path")

        if has_album(track) and (not compilation or compilations_in_albums):
            album_entry = albums.setdefault(
                (artist_key, album.casefold()),
                {
                    "title": album,
                    "artist": artist,
                    "track_count": 0,
                    "artwork_path": None,
                    "track": track,
                },
            )
            album_entry["track_count"] += 1

            if not album_entry["artwork_path"]:
                album_entry["artwork_path"] = track.get("album_artwork_path")

        if any(
            needle in normalize(track.get(field))
            for field in ("title", "album", "artist")
        ):
            (results["compilations"] if compilation else results["tracks"]).append(
                track
            )

    results["artists"] = sorted(
        (
            {
                "name": entry["name"],
                "album_count": len(entry["albums"]),
                "track_count": entry["track_count"],
                "artwork_path": entry["artwork_path"],
                "track": entry["track"],
            }
            for entry in artists.values()
            if needle in normalize(entry["name"])
        ),
        key=lambda item: _rank(item["name"], needle),
    )
    results["albums"] = sorted(
        (
            entry for entry in albums.values()
            if needle in normalize(entry["title"])
            or needle in normalize(entry["artist"])
        ),
        key=lambda item: _rank(item["title"], needle),
    )

    for key in ("tracks", "compilations"):
        results[key].sort(key=lambda track: _rank(track.get("title"), needle))

    return results
