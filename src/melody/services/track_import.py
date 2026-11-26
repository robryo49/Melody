"""Audio files waiting to be imported into the library.

Importing is two steps. First the files are read and shown to the person, who
may edit each one (title, artist, album, cover, start / end, compilation).
That is all kept here, in ``PendingTrack`` objects, and nothing is copied into
the library. Only when the person saves does ``LibraryEditor.import_track``
copy each file in and register it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from melody.services.library_scanner import SUPPORTED_EXTENSIONS
from melody.services.metadata import MetadataError, TrackMetadata, read_metadata


@dataclass
class PendingTrack:
    path: Path
    # What the file says about itself; what the person edits is below.
    metadata: TrackMetadata
    title: str
    artist: str
    album: str
    album_artist: str
    image: bytes | None = None
    start_time: float | None = None
    end_time: float | None = None
    kind: str | None = None
    # The track's ReplayGain, in dB, to write into its file when it is
    # imported (None: leave the file's tags alone), and the loudest sample
    # that goes with it. Measured when ReplayGain is on (see the window's
    # ``_measure_pending``), and changeable in the edit window.
    replaygain: float | None = None
    replaygain_peak: float | None = None
    # What the edit window shows for them: the year as the file gives it (it
    # may be a full date), and the genres joined (see services.genres).
    year: str = ""
    genre: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "PendingTrack":
        """Read ``path``. Raises ``MetadataError`` if it is not readable."""
        path = Path(path)
        metadata = read_metadata(path)

        return cls(
            path=path,
            metadata=metadata,
            title=metadata.title,
            artist=metadata.artist,
            album=metadata.album,
            album_artist=metadata.album_artist,
            year=metadata.year,
            genre=metadata.genre,
        )

    @classmethod
    def from_download(cls, download) -> "PendingTrack":
        """A track downloaded from the web (see ``melody.import_sources``).

        The audio file has no tags of its own, so the title, artist, album,
        year and cover come from the page it was downloaded from. They are
        starting values: the edit window lets the person change them.
        Raises ``MetadataError`` if the file is not readable.
        """
        pending = cls.from_file(download.path)

        if download.title:
            pending.title = download.title

        if download.artist:
            pending.artist = download.artist
            pending.album_artist = download.artist

        pending.album = download.album or ""

        if download.year:
            pending.year = download.year

        pending.image = download.image
        return pending

    def set_defaults(self, artist=None, album=None):
        """Start from the page the import was made from.

        ``artist`` becomes the track's artist and album artist, ``album`` its
        album (either left alone when None). They are only starting values:
        the edit window still lets the person change them.
        """
        if artist:
            self.artist = artist
            self.album_artist = artist

        if album is not None:
            self.album = album

    def final_metadata(self) -> TrackMetadata:
        """The tags the track gets in the library."""
        return TrackMetadata(
            title=self.title,
            artist=self.artist,
            album=self.album,
            album_artist=self.album_artist,
            duration=self.metadata.duration,
            year=self.year,
            genre=self.genre,
        )

    def as_track(self) -> dict:
        """The track as the library describes one, so the edit window and the
        list rows can show it like any other."""
        return {
            "id": None,
            "path": str(self.path),
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "duration": self.metadata.duration,
            "year": self.year,
            "genre": self.genre,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "kind": self.kind,
            "replaygain": self.replaygain,
        }

    def apply(self, values: dict):
        """Take what the edit window handed back."""
        # The file's own album artist is kept as long as the artist is left
        # alone. Changing the artist files the track under the new one, like
        # the edit window does for a track already in the library.
        if values["artist"] != self.artist:
            self.album_artist = values["artist"]

        self.title = values["title"]
        self.artist = values["artist"]
        self.album = values.get("album", "")

        # Absent when untouched: whatever was chosen before stays.
        if "image" in values:
            self.image = values["image"]

        if "start_time" in values:
            self.start_time = values["start_time"]
            self.end_time = values["end_time"]

        if "kind" in values:
            self.kind = values["kind"]

        if "replaygain" in values:
            self.replaygain = values["replaygain"]

        if "year" in values:
            self.year = values["year"]

        if "genre" in values:
            self.genre = values["genre"]


def load_pending_tracks(paths) -> tuple[list[PendingTrack], list[str]]:
    """Read every file in ``paths``.

    Returns the readable ones, and a message for each that is not (a file
    picked twice is only read once).
    """
    pending = []
    errors = []
    seen = set()

    for path in paths:
        path = Path(path)

        try:
            key = path.resolve()
        except OSError:
            key = path

        if key in seen:
            continue

        seen.add(key)

        if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            errors.append(f"{path.name}: not a supported audio file")
            continue

        try:
            pending.append(PendingTrack.from_file(path))
        except (MetadataError, OSError) as error:
            errors.append(f"{path.name}: {error}")

    return pending, errors
