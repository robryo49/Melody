"""Where a track starts and stops playing, and how long that makes it.

A track can have a custom ``start_time`` and/or ``end_time`` (seconds, kept in
the database; ``None`` means the very start / end of the file). Everything that
plays a track or shows its length goes through these two helpers, so they all
agree.
"""


def track_range(track):
    """``(start, end)`` in seconds for ``track``; ``end`` is ``None`` when the
    track plays to the end of the file."""
    if not track:
        return 0.0, None

    duration = float(track.get("duration") or 0)
    start = max(0.0, float(track.get("start_time") or 0))
    end = track.get("end_time")
    end = float(end) if end else None

    # Ignore values that no longer make sense (e.g. the file was replaced by
    # a shorter one).
    if duration > 0 and start >= duration:
        start = 0.0

    if end is not None and (
            end <= start or (duration > 0 and end >= duration - 0.05)
    ):
        end = None

    return start, end


def track_length(track):
    """How long ``track`` plays for, in seconds: its length between its start
    and end (the whole file's length if neither is set)."""
    if not track:
        return 0.0

    start, end = track_range(track)
    stop = end if end is not None else float(track.get("duration") or 0)

    return max(0.0, stop - start)
