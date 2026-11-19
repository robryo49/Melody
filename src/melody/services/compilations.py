"""Single tracks versus compilations.

When compilations are enabled in the preferences, an audio file longer than
the chosen length (15:00 by default) is a *compilation* (a mix, a live set...)
and everything else is a single track. A track can be switched from one to the
other by hand: that is kept in its ``kind`` (``None`` = automatic,
``"track"`` or ``"compilation"`` = forced).

The file's real length decides, not the length between its start and end
times, so trimming a compilation never turns it into a track.
"""

from melody.services.settings import get_settings


def automatic_is_compilation(track, settings=None):
    """Whether the track's length alone makes it a compilation."""
    settings = settings or get_settings()
    return float(track.get("duration") or 0) > settings.compilation_threshold


def is_compilation(track, settings=None):
    """Whether ``track`` is a compilation. Always False when compilations are
    switched off."""
    settings = settings or get_settings()

    if not settings.compilations_enabled:
        return False

    kind = track.get("kind")

    if kind == "compilation":
        return True

    if kind == "track":
        return False

    return automatic_is_compilation(track, settings)


def format_clock(seconds):
    """``15:00``, or ``1:15:00`` from an hour up."""
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"
