"""What an import source is.

A source is somewhere music can be imported from: files on this computer, a
link, a search... Each one is a class in a file of its own in this folder, and
is listed in ``__init__.py``. Adding a source means writing one class (and
listing it): the windows that use it are the same for all of them.

Sources contain no GTK code. They say how they are used with ``kind``:

* ``"files"``: the person picks audio files on this computer
  (``FileSource``).
* ``"link"``: the person pastes a link, and the source downloads it
  (``LinkSource``).
* ``"search"``: the person searches, picks a result, and the source
  downloads it like a link (``SearchSource``).

Whatever the source, what comes out is the same: ``PendingTrack`` objects
(see ``melody.services.track_import``). The person edits them, and only then
does ``LibraryEditor.import_track`` copy them into the library.

Everything that fetches something blocks: the windows run it in a thread.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from melody.services.track_import import PendingTrack


class ImportSourceError(Exception):
    """Something went wrong; the message is meant to be shown as it is."""


@dataclass
class Download:
    """Audio fetched from somewhere, waiting in a temporary folder.

    ``title``, ``artist``, ``album``, ``year`` and ``image`` (cover bytes) are
    whatever could be worked out about it: starting values for the edit
    window, any of them may be empty.
    """

    path: Path
    folder: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    image: bytes | None = None

    def cleanup(self):
        """Delete the temporary folder (and the audio file in it)."""
        shutil.rmtree(self.folder, ignore_errors=True)


@dataclass
class SearchResult:
    """One line of a search: ``url`` is what ``LinkSource.fetch`` downloads."""

    url: str
    title: str
    artist: str = ""
    duration: float | None = None
    thumbnail_url: str | None = None


class ImportSource:
    """The common part of every source."""

    # A short name, never shown.
    id = ""
    # The row in the "Import music" window.
    title = ""
    subtitle = ""
    icon_name = "folder-symbolic"
    # "files", "link" or "search" (set by the three classes below).
    kind = ""
    # A line shown under the source's windows ("" for none).
    notice = ""


class FileSource(ImportSource):
    """Audio files picked on this computer."""

    kind = "files"

    def load(self, paths) -> tuple[list[PendingTrack], list[str]]:
        """Read the files at ``paths``.

        Returns the tracks that can be imported, and a message for each file
        that cannot.
        """
        raise NotImplementedError


class LinkSource(ImportSource):
    """Audio downloaded from a link."""

    kind = "link"

    # The window that asks for the link.
    link_title = "Import from a link"
    link_label = "Link"
    prompt = "Paste a link."

    def accepts(self, url: str) -> bool:
        """Whether ``url`` is one this source can download."""
        raise NotImplementedError

    def fetch(self, url: str, on_progress=None, cancel=None) -> Download:
        """Download the audio at ``url`` into a temporary folder.

        ``on_progress(fraction, text)`` is called from the calling thread
        (``fraction`` is None while unknown). ``cancel`` is a
        ``threading.Event``: stop when it is set. Raises
        ``ImportSourceError`` with a message for the person.
        """
        raise NotImplementedError

    def to_pending(self, download: Download) -> PendingTrack:
        """The download as a track waiting to be imported. Raises
        ``MetadataError`` if the file is not readable."""
        return PendingTrack.from_download(download)


class SearchSource(LinkSource):
    """Audio found by searching. What it finds is downloaded like a link:
    ``accepts`` and ``fetch`` must work on its results' ``url``."""

    kind = "search"

    # The window that searches.
    search_title = "Search"
    search_placeholder = "Search…"

    def search(self, query: str) -> list[SearchResult]:
        """The results for ``query``. Raises ``ImportSourceError`` with a
        message for the person."""
        raise NotImplementedError

    def load_thumbnail(self, url: str) -> bytes:
        """The bytes of a result's picture (``SearchResult.thumbnail_url``)."""
        raise NotImplementedError
