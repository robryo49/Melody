"""Audio files already on this computer."""

from __future__ import annotations

from melody.import_sources.base import FileSource
from melody.services.track_import import load_pending_tracks


class LocalFileSource(FileSource):
    id = "file"
    title = "From file"
    subtitle = "Add audio files from this computer"
    icon_name = "document-open-symbolic"

    def load(self, paths):
        return load_pending_tracks(paths)
