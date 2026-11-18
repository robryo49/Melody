
import os
from dataclasses import dataclass
from pathlib import Path

from melody.services.metadata import MetadataError, TrackMetadata, read_metadata


SUPPORTED_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".m4b",
    ".aac",
    ".wav",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
    ".mpc",
}


@dataclass
class ScanEntry:
    path: Path
    destination: Path
    metadata: TrackMetadata


@dataclass
class ScanError:
    path: Path
    message: str


@dataclass
class ScanResult:
    entries: list[ScanEntry]
    errors: list[ScanError]


def _sanitize_name(value: str) -> str:
    invalid = '<>:"/\\|?*'

    name = "".join(
        "_" if char in invalid or ord(char) < 32 else char
        for char in value
    )

    name = name.strip().rstrip(".")

    if name in {"", ".", ".."}:
        return "Unknown"

    return name


def get_destination(root: Path, path: Path, metadata: TrackMetadata) -> Path:
    artist = _sanitize_name(metadata.album_artist)
    album = _sanitize_name(metadata.album)
    title = _sanitize_name(metadata.title)

    filename = f"{title}{path.suffix}"

    return root / artist / album / filename


def scan_library(root: Path) -> ScanResult:
    root = Path(root).expanduser().resolve()

    if not root.is_dir():
        raise NotADirectoryError(f"Music directory does not exist: {root}")

    entries = []
    errors = []

    for current_root, directories, filenames in os.walk(root):
        directories[:] = [
            directory
            for directory in directories
            if not directory.startswith(".")
        ]

        for filename in filenames:
            path = Path(current_root) / filename

            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            try:
                metadata = read_metadata(path)
                destination = get_destination(root, path, metadata)

                entries.append(
                    ScanEntry(
                        path=path,
                        destination=destination,
                        metadata=metadata,
                    )
                )

            except (MetadataError, OSError) as error:
                errors.append(
                    ScanError(path=path, message=str(error))
                )

    return ScanResult(entries=entries, errors=errors)
