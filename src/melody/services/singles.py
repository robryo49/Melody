"""Singles: tracks that do not belong to an album.

A track with no album tag is a *single*. It has no album folder either: its
file sits directly in its artist's folder (``Artist/Title.mp3``), and it is
listed under the artist's "Singles" rather than in any album.

A compilation (see ``compilations.py``) is a different thing: it is never a
single, even when it has no album.
"""

from melody.services.compilations import is_compilation


def has_album(track):
    """Whether ``track`` has an album at all."""
    return bool(str(track.get("album") or "").strip())


def is_single(track):
    """Whether ``track`` is a single: no album, and not a compilation."""
    return not has_album(track) and not is_compilation(track)
