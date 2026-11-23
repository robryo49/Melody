"""Looking images up on YouTube, for the image picker.

What comes out are ``ImageResult`` objects (see ``image_sources.py``): the
thumbnail of a video, or the picture of a channel. A video's thumbnail is
16:9; its middle square is what is used, like the covers of the YouTube
import.

The searching is done by yt-dlp, which Melody already uses to import audio.
It is imported only when a search is made, so Melody still starts without it.
Only search pages are read: no video is opened (a channel's page is only
read when its picture is not in the search). Everything here blocks, so the
picker runs it in threads.

What is asked of YouTube:

* ``search_music_videos`` (tracks): the "Videos" section of YouTube Music,
  which holds music videos and nothing else.
* ``search_album_covers`` (albums): the "Songs" section of YouTube Music. The
  video of a song there is YouTube's own upload of the release, and its
  thumbnail is the album's cover.
* ``search_artist_pictures`` (artists): YouTube's search, filtered to
  channels. A channel in the results already comes with its picture.
* ``search_videos`` (anything else): YouTube's search, as it is.

Each of the first three falls back on an ordinary YouTube search when YouTube
Music gives nothing: yt-dlp and YouTube change often, and a picker with
different results is better than one with none.
"""

from __future__ import annotations

import re
import sys
import threading
from urllib.parse import urlencode

from melody.services.artwork import (
    _center_square,
    _http_get,
    _normalize_text,
    _youtube_channel_picture,
    _youtube_thumbnail,
)
from melody.services.image_sources import ImageResult, ImageSearchError

MUSIC_SEARCH_URL = "https://music.youtube.com/search?"
SEARCH_URL = "https://www.youtube.com/results?"

# YouTube's own code for the search filter "Type: Channel".
CHANNEL_FILTER = "EgIQAg=="

# Longer than this, a video is a mix or a full album, not a music video (only
# checked on the ordinary search: YouTube Music's results have no length).
MAX_MUSIC_VIDEO_SECONDS = 15 * 60

_VIDEO_ID = re.compile(r"[\w-]{11}")
_CHANNEL_ID = re.compile(r"UC[\w-]{22}")


# ----------------------------------------------------------------------
# yt-dlp
# ----------------------------------------------------------------------

def _entries(url: str, limit: int) -> list[dict]:
    """What yt-dlp finds at ``url`` (a search address, or ``ytsearchN:...``),
    without opening any of it: at most ``limit`` dicts, with an id, a title
    and, for a video, its channel and length. Raises ``ImageSearchError``."""
    try:
        import yt_dlp
    except ImportError:
        raise ImageSearchError(
            "yt-dlp is not installed. Add it to Melody with: uv add yt-dlp"
        ) from None

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
        "playlistend": limit,
    }

    try:
        with yt_dlp.YoutubeDL(options) as searcher:
            info = searcher.extract_info(url, download=False)
    except Exception as error:
        message = re.sub(r"^ERROR:\s*", "", str(error)).strip()
        raise ImageSearchError(message or "The search failed.") from None

    return [
        entry for entry in (info or {}).get("entries") or [] if entry
    ][:limit]


def _quiet_entries(url: str, limit: int, what: str) -> list[dict]:
    """``_entries``, but a failure only says why on the console and gives
    nothing: for the searches that have something to fall back on."""
    try:
        return _entries(url, limit)
    except ImageSearchError as error:
        print(f"{what} failed: {error}", file=sys.stderr)
        return []


def _video_id(entry: dict) -> str | None:
    video_id = str(entry.get("id") or "")
    return video_id if _VIDEO_ID.fullmatch(video_id) else None


def _videos(entries: list[dict]) -> list[dict]:
    """The videos among ``entries`` (channels and playlists come out of a
    search too), each one once."""
    seen = set()
    videos = []

    for entry in entries:
        video_id = _video_id(entry)

        if video_id is None or video_id in seen:
            continue

        seen.add(video_id)
        videos.append(entry)

    return videos


def _channel_name(entry: dict) -> str:
    return str(entry.get("channel") or entry.get("uploader") or "")


def _is_topic(entry: dict) -> bool:
    """True for an upload of YouTube's auto-generated "Artist - Topic"
    channels: audio over the release's cover, not a video."""
    return _normalize_text(_channel_name(entry)).endswith(" topic")


def _music_section(query: str, section: str, limit: int) -> list[dict]:
    """The videos in a section ("videos", "songs") of the YouTube Music
    search. Nothing when it fails."""
    url = MUSIC_SEARCH_URL + urlencode({"q": query}) + "#" + section

    entries = _quiet_entries(
        url, limit, f"YouTube Music search for {query!r}",
    )

    return _videos(entries)[:limit]


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------

def _once(load):
    """``load`` (a function without arguments), run only the first time it is
    called: the same answer afterwards, even from another thread."""
    lock = threading.Lock()
    answer = []

    def loaded():
        with lock:
            if not answer:
                answer.append(load())

            return answer[0]

    return loaded


def _video_result(entry: dict) -> ImageResult:
    video_id = _video_id(entry)

    return ImageResult(
        title=str(entry.get("title") or "Untitled video"),
        detail=_channel_name(entry),
        # 320x180 and every video has one: quick to fetch for a list.
        load_thumbnail=lambda: _http_get(
            f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        ),
        # The big one, as a square.
        load_full=lambda: _youtube_thumbnail(video_id),
    )


def _avatar_urls(thumbnails: list[dict] | None, size: int) -> list[str]:
    """The https addresses of a channel's picture at about ``size`` pixels,
    the biggest of the pictures YouTube listed first.

    A search lists the picture at 88 pixels, but the number in its address is
    only a request: the same address works at any size.
    """
    urls = []

    for thumbnail in sorted(
            thumbnails or [],
            key=lambda item: item.get("width") or 0,
            reverse=True,
    ):
        url = str(thumbnail.get("url") or "")

        # yt-dlp may leave off the scheme.
        if url.startswith("//"):
            url = "https:" + url

        if url.startswith("https://"):
            urls.append(re.sub(r"=s\d+", f"=s{size}", url, count=1))

    return urls


def _channel_result(
        channel_id: str,
        name: str,
        thumbnails: list[dict] | None,
) -> ImageResult:
    # Reading the channel itself is slow, so it is a last resort, and done
    # once for both uses.
    from_channel = _once(lambda: _youtube_channel_picture(channel_id))

    def load(size):
        for url in _avatar_urls(thumbnails, size):
            try:
                return _center_square(_http_get(url))
            except Exception:
                continue

        return from_channel()

    return ImageResult(
        title=name or "YouTube channel",
        detail="YouTube channel",
        load_thumbnail=lambda: load(256),
        load_full=lambda: load(800),
    )


# ----------------------------------------------------------------------
# The searches
# ----------------------------------------------------------------------

def search_music_videos(query: str, limit: int = 12) -> list[ImageResult]:
    """Music videos for ``query`` ("title artist")."""
    entries = _music_section(query, "videos", limit)

    if not entries:
        # An ordinary search, narrowed to what looks like a music video: not
        # a live stream, not a mix, not audio over a cover.
        found = _videos(
            _entries(
                f"ytsearch{limit * 2}:{query} official music video",
                limit * 2,
            )
        )
        entries = [
            entry for entry in found
            if not _is_topic(entry)
            and entry.get("live_status") not in ("is_live", "is_upcoming")
            and (entry.get("duration") or 0) <= MAX_MUSIC_VIDEO_SECONDS
        ][:limit]

    return [_video_result(entry) for entry in entries]


def search_album_covers(query: str, limit: int = 16) -> list[ImageResult]:
    """Covers for ``query`` ("artist album"), as the thumbnails of the songs
    YouTube Music finds. Several songs of one album show the same cover: the
    picker leaves the repeats out."""
    entries = _music_section(query, "songs", limit)

    if not entries:
        # An ordinary search: the uploads of "Artist - Topic" channels first,
        # as their thumbnail is the cover itself; then the other videos, so
        # there is nearly always something to offer, even if it is a frame
        # of a video and not the cover.
        found = _videos(_entries(f"ytsearch{limit}:{query}", limit))
        entries = sorted(found, key=lambda entry: not _is_topic(entry))

    return [_video_result(entry) for entry in entries]


def _channels_of(entries: list[dict]) -> list[tuple[str, str, list]]:
    """``(id, name, pictures)`` of the channels in a channel search, each one
    once."""
    channels = {}

    for entry in entries:
        channel_id = str(entry.get("id") or entry.get("channel_id") or "")

        if _CHANNEL_ID.fullmatch(channel_id) and channel_id not in channels:
            channels[channel_id] = (
                str(entry.get("title") or entry.get("channel") or ""),
                entry.get("thumbnails") or [],
            )

    return [
        (channel_id, name, thumbnails)
        for channel_id, (name, thumbnails) in channels.items()
    ]


def search_artist_pictures(query: str, limit: int = 8) -> list[ImageResult]:
    """Pictures of the YouTube channels found for ``query`` (an artist's
    name), their "- Topic" and official channels being the likeliest to show
    the artist."""
    url = SEARCH_URL + urlencode({
        "search_query": query,
        "sp": CHANNEL_FILTER,
    })

    channels = _channels_of(
        _quiet_entries(url, limit, f"YouTube channel search for {query!r}")
    )

    if not channels:
        # The channels of the videos an ordinary search finds, the artist's
        # own first.
        expected = _normalize_text(query)
        videos = _entries(f"ytsearch10:{query}", 10)

        def rank(entry):
            name = _normalize_text(_channel_name(entry))
            return name not in (expected, f"{expected} topic")

        channels = _channels_of([
            {"id": entry.get("channel_id"), "title": _channel_name(entry)}
            for entry in sorted(videos, key=rank)
        ])

    return [
        _channel_result(channel_id, name, thumbnails)
        for channel_id, name, thumbnails in channels[:limit]
    ]


def search_videos(query: str, limit: int = 12) -> list[ImageResult]:
    """The videos YouTube finds for ``query``."""
    entries = _videos(_entries(f"ytsearch{limit}:{query}", limit))

    return [_video_result(entry) for entry in entries[:limit]]
