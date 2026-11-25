"""The audio formats a download can be saved as (see ``import_sources``).

A YouTube video's audio is converted to the format chosen in the
preferences, with ffmpeg. All but Opus can hold a ReplayGain tag.
"""

# key (what the setting stores) -> what to show, and what yt-dlp is told.
IMPORT_FORMATS = {
    "mp3": {
        "label": "MP3",
        "codec": "mp3",
        "note": "Plays everywhere.",
    },
    "m4a": {
        "label": "M4A (AAC)",
        "codec": "m4a",
        "note": "Smaller files, at the same quality as MP3.",
    },
    "ogg": {
        "label": "Ogg Vorbis",
        "codec": "vorbis",
        "note": "An open format.",
    },
    "opus": {
        "label": "Opus",
        "codec": "opus",
        "note": "The best quality for its size, but it cannot hold a "
                "ReplayGain tag.",
    },
}

DEFAULT_IMPORT_FORMAT = "mp3"

# What the converted audio is encoded at (kbit/s). YouTube's own audio is
# around 128 to 160, so this keeps what there is without adding losses.
BITRATE = "256"


def import_format(key):
    """The settings of format ``key`` (the default one for an unknown key):
    ``{"key", "label", "codec", "note"}``."""
    if key not in IMPORT_FORMATS:
        key = DEFAULT_IMPORT_FORMAT

    return {"key": key, **IMPORT_FORMATS[key]}
