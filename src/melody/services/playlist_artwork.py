"""The picture a person chose for a playlist.

A playlist without one shows the cover of its first track (see
``views/playlists.py``). The picture is kept as a file next to the database
(``Database.playlist_cover_path``), so nothing in the database changes, and it
is removed together with the playlist.

Everything here is GTK-free.
"""

from __future__ import annotations

from melody.services.artwork import write_cover_tile
from melody.services.artwork_editor import ArtworkError, prepare_image

# Big enough to stay sharp on the playlist's page and in the grid.
COVER_SIZE = 512


def get_playlist_cover(database, playlist_id: str) -> str | None:
    """Path of the picture chosen for the playlist (None if it has none)."""
    path = database.playlist_cover_path(playlist_id)
    return str(path) if path.is_file() else None


def set_playlist_cover(database, playlist_id: str, data: bytes) -> None:
    """Make ``data`` (any readable image) the playlist's picture.

    Raises ``ArtworkError`` if it is not an image or cannot be saved.
    """
    # Straightened (EXIF rotation) and checked first, so an unreadable image
    # gives a clear message and never replaces the current picture.
    image, _mime = prepare_image(data)

    try:
        write_cover_tile(
            image,
            database.playlist_cover_path(playlist_id),
            COVER_SIZE,
        )
    except Exception as error:
        raise ArtworkError(
            f"Could not save the playlist's picture: {error}"
        ) from error
