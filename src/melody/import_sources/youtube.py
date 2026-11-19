"""YouTube: the audio of a video, by link or by search.

Two sources live here: ``YouTubeLinkSource`` (paste a link) and
``YouTubeSearchSource`` (search, then pick a video). Both download with
``download_audio``, which fetches one video's audio into a temporary folder
and returns a ``Download``: the audio file, plus the title / artist / album /
cover that could be worked out from the video.

The download itself is done by ``yt-dlp``, imported only when it is used so
Melody still starts without it.

Only download what you have the right to: YouTube's terms of service do not
allow downloading, and most music on it is copyrighted.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from melody.import_sources.base import (
    Download,
    ImportSourceError,
    LinkSource,
    SearchResult,
    SearchSource,
)
from melody.services.library_scanner import SUPPORTED_EXTENSIONS


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

NOTICE = (
    "Only import audio you have the right to use: YouTube's terms do not "
    "allow downloading, and most music is copyrighted."
)

# "(Official Video)", "[Lyrics]", "(HD)"... : noise in a video title, not part
# of the song's name.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official\s*)?(?:music\s*)?"
    r"(?:video|audio|lyrics?|lyric\s+video|visuali[sz]er|hd|hq|4k|mv)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


class _Cancelled(Exception):
    pass


def is_youtube_url(text: str) -> bool:
    try:
        parsed = urlparse(str(text).strip())
    except ValueError:
        return False

    return (
        parsed.scheme in ("http", "https")
        and (parsed.hostname or "") in YOUTUBE_HOSTS
    )


def _is_playlist_url(text: str) -> bool:
    parsed = urlparse(text.strip())
    return parsed.path.rstrip("/") == "/playlist"


def _tags_from_info(info: dict) -> tuple[str, str, str, str]:
    """(title, artist, album, year) for a video.

    YouTube Music's own uploads carry real ``track`` / ``artist`` / ``album``
    fields. For an ordinary video, "Artist - Title" is split when the title
    has that shape; otherwise the channel is the artist. All of it can be
    changed in the edit window anyway.
    """
    title = str(info.get("track") or info.get("title") or "").strip()
    artist = str(info.get("artist") or info.get("creator") or "").strip()
    album = str(info.get("album") or "").strip()

    if not artist:
        left, separator, right = title.partition(" - ")

        if separator and left.strip() and right.strip():
            artist, title = left.strip(), right.strip()
        else:
            channel = str(
                info.get("channel") or info.get("uploader") or ""
            )
            artist = re.sub(r"\s*-\s*Topic$", "", channel).strip()

    title = _TITLE_NOISE.sub("", title).strip() or title

    year = str(info.get("release_year") or "").strip()

    if not year:
        year = str(info.get("upload_date") or "")[:4]

    return title, artist, album, year


def _square_cover(data: bytes) -> bytes:
    """Centre-crop a video thumbnail (16:9) to a square cover, as JPEG."""
    from PIL import Image

    with Image.open(BytesIO(data)) as source:
        image = source.convert("RGB")

    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))

    output = BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


def _fetch_thumbnail(info: dict) -> bytes | None:
    url = info.get("thumbnail")

    if not url:
        thumbnails = info.get("thumbnails") or []
        url = thumbnails[-1].get("url") if thumbnails else None

    if not url or not str(url).startswith("https://"):
        return None

    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})

        with urlopen(request, timeout=15) as response:
            return _square_cover(response.read(10_000_000))
    except Exception:
        # The cover is a nicety: the track can do without it.
        return None


def download_audio(
        url: str,
        on_progress=None,
        cancel: threading.Event | None = None,
) -> Download:
    """Download the audio of the YouTube video at ``url``.

    ``on_progress(fraction, text)`` is called from the calling thread while
    the audio comes in (``fraction`` is None while its size is unknown). Set
    ``cancel`` to stop it. Raises ``ImportSourceError`` with a message that
    can be shown to the person.
    """
    url = str(url).strip()

    if not is_youtube_url(url):
        raise ImportSourceError("That does not look like a YouTube link.")

    if _is_playlist_url(url):
        raise ImportSourceError(
            "That link is a playlist. Paste the link of a single video."
        )

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise ImportSourceError(
            "yt-dlp is not installed. Add it to Melody with: uv add yt-dlp"
        ) from None

    def report(fraction, text):
        if on_progress is not None:
            on_progress(fraction, text)

    def hook(status):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()

        if status.get("status") == "downloading":
            total = (
                status.get("total_bytes")
                or status.get("total_bytes_estimate")
            )
            done = status.get("downloaded_bytes") or 0
            report(done / total if total else None, "Downloading…")
        elif status.get("status") == "finished":
            report(1.0, "Finishing…")

    # The audio is converted to Opus with ffmpeg when it is installed: it is
    # what GStreamer decodes with its base plugins, and YouTube's best audio
    # is Opus already, so it is usually just repackaged, not re-encoded.
    # Without ffmpeg, YouTube's own AAC (m4a) stream is kept as it is, which
    # needs GStreamer's AAC decoder.
    has_ffmpeg = shutil.which("ffmpeg") is not None

    options = {
        "format": (
            "bestaudio/best"
            if has_ffmpeg
            else "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]"
        ),
        "outtmpl": "%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "socket_timeout": 20,
        "retries": 3,
    }

    if has_ffmpeg:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "opus"}
        ]

    folder = Path(tempfile.mkdtemp(prefix="melody-youtube-"))
    options["paths"] = {"home": str(folder)}
    succeeded = False

    try:
        report(None, "Looking the video up…")

        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)

            if not info or info.get("entries"):
                raise ImportSourceError(
                    "Could not find a single video at that link."
                )

            if info.get("is_live"):
                raise ImportSourceError(
                    "That is a live stream: wait until it has ended."
                )

            downloader.process_ie_result(info, download=True)

        files = sorted(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
        )

        if not files:
            raise ImportSourceError(
                "The audio could not be saved in a format Melody supports. "
                "Installing ffmpeg usually fixes this."
            )

        report(None, "Reading the video's details…")
        title, artist, album, year = _tags_from_info(info)

        succeeded = True

        return Download(
            path=files[0],
            folder=folder,
            title=title,
            artist=artist,
            album=album,
            year=year,
            image=_fetch_thumbnail(info),
        )

    except _Cancelled:
        raise ImportSourceError("Cancelled.") from None
    except ImportSourceError:
        raise
    except DownloadError as error:
        if cancel is not None and cancel.is_set():
            raise ImportSourceError("Cancelled.") from None

        message = re.sub(r"^ERROR:\s*", "", str(error)).strip()

        raise ImportSourceError(
            f"{message}\n\nYouTube changes often: if this keeps happening "
            "for every video, update yt-dlp (uv lock --upgrade-package "
            "yt-dlp, then uv sync)."
        ) from None
    except Exception as error:
        raise ImportSourceError(str(error)) from None
    finally:
        if not succeeded:
            shutil.rmtree(folder, ignore_errors=True)


def search_videos(query: str, limit: int = 15) -> list[SearchResult]:
    """Search YouTube for videos matching ``query``.

    Only the search page is read (no video is opened), so it is quick. The
    title and artist are worked out the same way as for a download, so what
    the list shows is what the edit window will start from. Raises
    ``ImportSourceError`` with a message for the person.
    """
    query = str(query).strip()

    if not query:
        raise ImportSourceError("Type something to search for.")

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise ImportSourceError(
            "yt-dlp is not installed. Add it to Melody with: uv add yt-dlp"
        ) from None

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
    }

    try:
        with yt_dlp.YoutubeDL(options) as searcher:
            info = searcher.extract_info(
                f"ytsearch{int(limit)}:{query}",
                download=False,
            )
    except DownloadError as error:
        message = re.sub(r"^ERROR:\s*", "", str(error)).strip()
        raise ImportSourceError(message or "The search failed.") from None
    except Exception as error:
        raise ImportSourceError(str(error) or "The search failed.") from None

    results = []

    for entry in (info or {}).get("entries") or []:
        video_id = str(entry.get("id") or "")

        if not re.fullmatch(r"[\w-]{11}", video_id):
            continue

        if entry.get("live_status") in ("is_live", "is_upcoming"):
            continue

        title, artist, _album, _year = _tags_from_info(entry)

        results.append(
            SearchResult(
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title or "Unknown Title",
                artist=artist,
                duration=entry.get("duration"),
                # A 320x180 JPEG, which every video has.
                thumbnail_url=(
                    f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
                ),
            )
        )

    return results


def fetch_image(url: str) -> bytes:
    """The bytes of an image at an https ``url`` (a search result's
    thumbnail)."""
    if not str(url).startswith("https://"):
        raise ImportSourceError("Not a secure image address.")

    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urlopen(request, timeout=15) as response:
        return response.read(5_000_000)


# ----------------------------------------------------------------------
# The sources
# ----------------------------------------------------------------------

class YouTubeLinkSource(LinkSource):
    id = "youtube-link"
    title = "From YouTube link"
    subtitle = "Download the audio of a video"
    icon_name = "insert-link-symbolic"
    notice = NOTICE

    link_title = "Import from YouTube"
    link_label = "YouTube link"
    prompt = "Paste the link of a YouTube video."

    def accepts(self, url):
        return is_youtube_url(url)

    def fetch(self, url, on_progress=None, cancel=None):
        return download_audio(url, on_progress, cancel)


class YouTubeSearchSource(SearchSource):
    id = "youtube-search"
    title = "Search YouTube"
    subtitle = "Look a song up and pick it from the results"
    icon_name = "system-search-symbolic"
    notice = NOTICE

    # The window that downloads what was picked.
    link_title = "Import from YouTube"
    link_label = "YouTube link"
    prompt = "Paste the link of a YouTube video."

    search_title = "Search YouTube"
    search_placeholder = "Search YouTube…"

    def accepts(self, url):
        return is_youtube_url(url)

    def fetch(self, url, on_progress=None, cancel=None):
        return download_audio(url, on_progress, cancel)

    def search(self, query):
        return search_videos(query)

    def load_thumbnail(self, url):
        return fetch_image(url)
