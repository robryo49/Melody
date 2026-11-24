"""ReplayGain: a track's volume correction, kept in the file's tags.

A track's ``REPLAYGAIN_TRACK_GAIN`` tag says how many decibels to turn it up
(+) or down (-) so tracks recorded at different levels sound equally loud.
Players that know about it (Melody, when ReplayGain is on) apply it. The tags
that already exist in a file are used as they are, and the edit window can
write one for a file that has none.

Everything here is GTK-free. Supported are the files that carry the tag the
usual way: MP3 (ID3 "TXXX" frames), FLAC and Ogg Vorbis (Vorbis comments),
and MP4 / M4A (iTunes free-form atoms). Opus does not use these tags (it has
its own, in other units), so it is left out.
"""

import re

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggvorbis import OggVorbis

# The most a gain can be turned up or down, in dB: what the edit window
# accepts, and what the player applies at most.
MAX_GAIN = 24.0

_MP4_PREFIX = "----:com.apple.iTunes:"
_NUMBER = re.compile(r"\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:db)?\s*", re.IGNORECASE)


class ReplayGainError(Exception):
    pass


def _kind(audio):
    """How ``audio`` stores ReplayGain tags: "id3", "mp4" or "vorbis", or
    None when it cannot."""
    if isinstance(audio, MP3):
        return "id3"

    if isinstance(audio, MP4):
        return "mp4"

    if isinstance(audio, (FLAC, OggVorbis)):
        return "vorbis"

    return None


def _number(text):
    """The number in a tag's text ("-6.50 dB", "0.98"), or None."""
    match = _NUMBER.fullmatch(str(text or ""))

    if match is None:
        return None

    return float(match.group(1).replace(",", "."))


def _read_tag(audio, kind, name):
    """The text of the tag ``name`` ("track_gain", "album_peak"...), whatever
    case it was written in, or None."""
    wanted = f"replaygain_{name}"
    tags = audio.tags

    if tags is None:
        return None

    if kind == "id3":
        for frame in tags.getall("TXXX"):
            if frame.desc.casefold() == wanted and frame.text:
                return str(frame.text[0])

    elif kind == "mp4":
        for key, values in tags.items():
            if key.casefold() == f"{_MP4_PREFIX}{wanted}".casefold() and values:
                value = values[0]

                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")

                return str(value)

    elif kind == "vorbis":
        for key in tags.keys():
            if key.casefold() == wanted:
                values = tags[key]

                if values:
                    return str(values[0])

    return None


def _open(path):
    try:
        return MutagenFile(path)
    except Exception:
        return None


def supports_replaygain(path):
    """Whether ``path`` is a file that can hold a ReplayGain tag."""
    audio = _open(path)
    return audio is not None and _kind(audio) is not None


def read_track_gain(path):
    """The track gain in dB tagged in the file, or None when it has none (or
    cannot be read). This is what the edit window shows."""
    audio = _open(path)
    kind = _kind(audio) if audio is not None else None

    if kind is None:
        return None

    try:
        return _number(_read_tag(audio, kind, "track_gain"))
    except Exception:
        return None


def read_replaygain(path):
    """``(gain, peak)`` to play the file with: the track's own tags, or its
    album's when it has only those. ``gain`` is in dB and ``peak`` the loudest
    sample (1.0 = full scale); either is None when the file has no such tag."""
    audio = _open(path)
    kind = _kind(audio) if audio is not None else None

    if kind is None:
        return None, None

    try:
        gain = _number(_read_tag(audio, kind, "track_gain"))
        peak = _number(_read_tag(audio, kind, "track_peak"))

        if gain is None:
            gain = _number(_read_tag(audio, kind, "album_gain"))
            peak = _number(_read_tag(audio, kind, "album_peak"))
    except Exception:
        return None, None

    return gain, peak


def write_track_gain(path, gain, peak=None):
    """Tag the file with a track gain of ``gain`` dB, or, with ``None``, take
    its track gain tag away. Raises ``ReplayGainError`` if it cannot be done.

    With ``peak`` (the loudest sample, 1.0 = full scale) the track peak tag is
    written too, which lets a player keep a boost from clipping. Its other
    tags (the album's gain...) are left as they are.
    """
    audio = _open(path)

    if audio is None:
        raise ReplayGainError("Unsupported or unreadable audio file")

    kind = _kind(audio)

    if kind is None:
        raise ReplayGainError(
            "This type of file cannot hold a ReplayGain tag"
        )

    if audio.tags is None:
        if gain is None:
            return

        try:
            audio.add_tags()
        except Exception as error:
            raise ReplayGainError(f"Could not create tags: {error}") from error

    # What to write: tag name -> text, None meaning "take it away".
    values = {
        "track_gain": None if gain is None else f"{float(gain):.2f} dB",
    }

    if gain is not None and peak is not None:
        values["track_peak"] = f"{float(peak):.6f}"

    try:
        for name, text in values.items():
            wanted = f"replaygain_{name}"

            if kind == "id3":
                for frame in list(audio.tags.getall("TXXX")):
                    if frame.desc.casefold() == wanted:
                        audio.tags.delall(frame.HashKey)

                if text is not None:
                    audio.tags.add(
                        TXXX(encoding=3, desc=wanted.upper(), text=[text])
                    )

            elif kind == "mp4":
                for key in list(audio.tags.keys()):
                    if key.casefold() == f"{_MP4_PREFIX}{wanted}".casefold():
                        del audio.tags[key]

                if text is not None:
                    audio.tags[f"{_MP4_PREFIX}{wanted}"] = [
                        MP4FreeForm(text.encode("utf-8"))
                    ]

            else:
                for key in list(audio.tags.keys()):
                    if key.casefold() == wanted:
                        del audio.tags[key]

                if text is not None:
                    audio.tags[wanted.upper()] = [text]

        audio.save()
    except Exception as error:
        raise ReplayGainError(f"Could not write the tag: {error}") from error


def format_gain(gain):
    """A gain as shown in the edit window: ``-6.5``, ``3``, ``0``."""
    text = f"{float(gain):.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def parse_gain(text):
    """What the person typed as a gain, in dB (``"-6.5"``, ``"-6.5 dB"``), or
    None when it is empty. Raises ValueError for anything else."""
    text = str(text or "").strip()

    if not text:
        return None

    gain = _number(text)

    if gain is None or abs(gain) > MAX_GAIN:
        raise ValueError(
            f"The ReplayGain must be a number of dB from -{MAX_GAIN:g} "
            f"to +{MAX_GAIN:g}."
        )

    return gain


def is_valid_gain_text(text):
    """Whether ``parse_gain`` accepts ``text`` (an empty one means "none")."""
    try:
        parse_gain(text)
    except ValueError:
        return False

    return True
