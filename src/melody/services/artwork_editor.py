"""Writing images: cover art embedded in audio files, artist portraits.

Everything here is GTK-free so it can be used (and tested) on its own.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3FileType
from mutagen.mp4 import MP4, MP4Cover
from mutagen.ogg import OggFileType
from mutagen.wave import WAVE
from PIL import Image, ImageOps

MAX_IMAGE_SIDE = 1200


class ArtworkError(Exception):
    pass


def prepare_image(
        data: bytes,
        *,
        max_side: int = MAX_IMAGE_SIDE,
        jpeg_only: bool = False,
) -> tuple[bytes, str]:
    """Normalise any readable image to JPEG (or PNG if it has transparency).

    The picture is rotated according to its EXIF data and scaled down to at
    most ``max_side`` pixels, so embedded covers stay a reasonable size.
    Returns ``(bytes, mime_type)``.
    """
    try:
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            has_alpha = (
                    image.mode in ("RGBA", "LA", "PA")
                    or (image.mode == "P" and "transparency" in image.info)
            )
            image = image.convert("RGBA" if has_alpha else "RGB")
    except Exception as error:
        raise ArtworkError("That file is not a readable image") from error

    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    output = BytesIO()

    if has_alpha and not jpeg_only:
        image.save(output, format="PNG", optimize=True)
        return output.getvalue(), "image/png"

    if has_alpha:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background

    image.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue(), "image/jpeg"


def _make_picture(data: bytes, mime: str) -> Picture:
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        depth = 32 if image.mode == "RGBA" else 24

    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = mime
    picture.desc = "Cover"
    picture.width = width
    picture.height = height
    picture.depth = depth
    picture.data = data
    return picture


def embed_artwork(path: Path, data: bytes, mime: str):
    """Replace the cover art embedded in ``path`` with ``data``.

    ``data`` should come from :func:`prepare_image`.
    """
    path = Path(path)

    try:
        audio = MutagenFile(path)
    except Exception as error:
        raise ArtworkError(f"Could not open {path.name}: {error}") from error

    if audio is None:
        raise ArtworkError(f"Unsupported or unreadable file: {path.name}")

    try:
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(_make_picture(data, mime))

        elif isinstance(audio, MP4):
            image_format = (
                MP4Cover.FORMAT_PNG
                if mime == "image/png"
                else MP4Cover.FORMAT_JPEG
            )
            audio["covr"] = [MP4Cover(data, imageformat=image_format)]

        elif isinstance(audio, OggFileType):
            picture = _make_picture(data, mime)
            for legacy in ("coverart", "coverartmime"):
                if legacy in audio:
                    del audio[legacy]
            audio["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii")
            ]

        elif isinstance(audio, (ID3FileType, WAVE, AIFF)):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=mime,
                    type=3,
                    desc="Cover",
                    data=data,
                )
            )

        else:
            raise ArtworkError(
                f"Embedding a cover is not supported for "
                f"{path.suffix or 'this'} files"
            )

        audio.save()

    except ArtworkError:
        raise
    except Exception as error:
        raise ArtworkError(
            f"Could not write the cover of {path.name}: {error}"
        ) from error


def write_folder_image(folder: Path, data: bytes, name: str) -> Path:
    """Save ``data`` as ``name`` in ``folder`` (atomically).

    ``data`` should come from :func:`prepare_image` with ``jpeg_only=True``.
    """
    folder = Path(folder)
    final = folder / name
    temporary = folder / f"{name}.tmp"

    try:
        temporary.write_bytes(data)
        temporary.replace(final)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ArtworkError(f"Could not save {name}: {error}") from error

    return final


def write_artist_image(folder: Path, data: bytes) -> Path:
    """``artist.jpg`` is the first name Melody looks for in an artist's folder."""
    return write_folder_image(folder, data, "artist.jpg")


def write_cover_image(folder: Path, data: bytes) -> Path:
    """``cover.jpg`` is the first name Melody looks for in an album's folder."""
    return write_folder_image(folder, data, "cover.jpg")
