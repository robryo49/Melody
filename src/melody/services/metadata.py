from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile


class MetadataError(Exception):
    pass


@dataclass
class TrackMetadata:
    title: str
    artist: str
    album: str
    album_artist: str
    duration: float
    year: str
    genre: str


def _get_tag(tags, key: str, default: str = "") -> str:
    if not tags:
        return default

    value = tags.get(key)
    if not value:
        return default

    if isinstance(value, (list, tuple)):
        value = value[0] if value else default

    return str(value).strip() if value is not None else default


def read_metadata(path: Path) -> TrackMetadata:
    path = Path(path)

    try:
        audio = MutagenFile(path, easy=True)
    except Exception as error:
        raise MetadataError(f"Could not read metadata: {error}") from error

    if audio is None:
        raise MetadataError("Unsupported or unreadable audio file")

    if audio.info is None:
        raise MetadataError("Audio file has no readable audio information")

    tags = audio.tags or {}

    title = _get_tag(tags, "title", path.stem)
    artist = _get_tag(tags, "artist", "Unknown Artist")
    album = _get_tag(tags, "album", "Unknown Album")
    album_artist = _get_tag(tags, "albumartist", artist)

    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        duration=float(audio.info.length or 0),
        year=_get_tag(tags, "date"),
        genre=_get_tag(tags, "genre"),
    )


def write_metadata(path: Path, metadata: TrackMetadata):
    path = Path(path)

    try:
        audio = MutagenFile(path, easy=True)
    except Exception as error:
        raise MetadataError(f"Could not open file for editing: {error}") from error

    if audio is None:
        raise MetadataError("Unsupported or unreadable audio file")

    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as error:
            raise MetadataError(f"Could not create metadata tags: {error}") from error

    values = {
        "title": metadata.title,
        "artist": metadata.artist,
        "album": metadata.album,
        "albumartist": metadata.album_artist,
        "date": metadata.year,
        "genre": metadata.genre,
    }

    try:
        for key, value in values.items():
            if value:
                audio[key] = [str(value)]
            else:
                audio.pop(key, None)

        audio.save()
    except Exception as error:
        raise MetadataError(f"Could not write metadata: {error}") from error