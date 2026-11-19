"""Where music can be imported from.

Each source is a class in a file of its own here (see ``base.py`` for what a
source is). ``get_import_sources()`` lists the ones offered in the "Import
music" window, in that order. To add a source: write its class in a new file,
and add it below.
"""

from __future__ import annotations

from melody.features import YOUTUBE_IMPORT_ENABLED
from melody.import_sources.base import (
    Download,
    FileSource,
    ImportSource,
    ImportSourceError,
    LinkSource,
    SearchResult,
    SearchSource,
)
from melody.import_sources.file import LocalFileSource

__all__ = [
    "Download",
    "FileSource",
    "ImportSource",
    "ImportSourceError",
    "LinkSource",
    "SearchResult",
    "SearchSource",
    "get_import_sources",
]


def get_import_sources() -> list[ImportSource]:
    sources = [LocalFileSource()]

    if YOUTUBE_IMPORT_ENABLED:
        # Imported here, and allowed to be missing: a build without the
        # feature can leave ``youtube.py`` out (see ``melody.features``).
        try:
            from melody.import_sources.youtube import (
                YouTubeLinkSource,
                YouTubeSearchSource,
            )
        except ModuleNotFoundError as error:
            if error.name != "melody.import_sources.youtube":
                raise
        else:
            sources.extend([YouTubeLinkSource(), YouTubeSearchSource()])

    return sources
