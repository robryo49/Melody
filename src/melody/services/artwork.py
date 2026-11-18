
import base64
import hashlib
import json
import os
from datetime import timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from mutagen import File as MutagenFile
from mutagen.flac import Picture
from PIL import Image, ImageDraw, ImageOps


CACHE_HOME = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
)
ARTWORK_CACHE = CACHE_HOME / "melody" / "artwork"
LOOKUP_CACHE = ARTWORK_CACHE / "musicbrainz.json"
ARTIST_LOOKUP_CACHE = ARTWORK_CACHE / "artists.json"
ARTIST_IMAGE_CACHE = ARTWORK_CACHE / "artists"

USER_AGENT = "Melody/0.1.0 (desktop music library application)"

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/release/"
MUSICBRAINZ_ARTIST_URL = "https://musicbrainz.org/ws/2/artist/"
COVER_ART_URL = "https://coverartarchive.org/release/"
DEEZER_URL = "https://api.deezer.com"
FANART_URL = "https://webservice.fanart.tv/v3.2/music/"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"

FANART_API_KEY = (
        os.environ.get("MELODY_FANART_API_KEY")
        or os.environ.get("FANART_API_KEY")
)

NEGATIVE_CACHE_DAYS = 30
MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024
ARTIST_LOOKUP_VERSION = 2

_request_lock = threading.Lock()
_last_musicbrainz_request = 0.0
_last_wikidata_request = 0.0


def _get_artwork_data(path: Path) -> bytes | None:
    audio = MutagenFile(path)

    if audio is None or audio.tags is None:
        return None

    tags = audio.tags

    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data

    if hasattr(tags, "getall"):
        pictures = tags.getall("APIC")
        if pictures:
            return pictures[0].data

    covers = tags.get("covr")
    if covers:
        return bytes(covers[0])

    encoded_picture = tags.get("metadata_block_picture")
    if encoded_picture:
        if isinstance(encoded_picture, (list, tuple)):
            encoded_picture = encoded_picture[0]

        return Picture(base64.b64decode(encoded_picture)).data

    coverart = tags.get("coverart")
    if coverart:
        if isinstance(coverart, (list, tuple)):
            coverart = coverart[0]

        return base64.b64decode(coverart)

    return None


def _get_local_artwork(path: Path) -> bytes | None:
    directory = path.parent

    preferred_names = [
        "cover.jpg",
        "cover.jpeg",
        "cover.png",
        "folder.jpg",
        "folder.jpeg",
        "folder.png",
        "front.jpg",
        "front.jpeg",
        "front.png",
        "album.jpg",
        "album.jpeg",
        "album.png",
    ]

    for name in preferred_names:
        candidate = directory / name

        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                pass

    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}

    try:
        candidates = sorted(
            candidate
            for candidate in directory.iterdir()
            if candidate.is_file()
            and candidate.suffix.casefold() in image_extensions
        )
    except OSError:
        return None

    for candidate in candidates:
        try:
            return candidate.read_bytes()
        except OSError:
            pass

    return None


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _album_cache_key(artist: str, album: str) -> str:
    normalized = (
        f"{_normalize_text(artist)}:{_normalize_text(album)}"
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _load_json_cache(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except (OSError, ValueError):
        return {}


def _save_json_cache(path: Path, cache: dict):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)

    temporary_path.replace(path)


def _get_musicbrainz_release_id(path: Path) -> str | None:
    audio = MutagenFile(path, easy=True)

    if audio is None or audio.tags is None:
        return None

    for key in (
            "musicbrainz_albumid",
            "musicbrainz_releaseid",
            "MUSICBRAINZ_ALBUMID",
            "MUSICBRAINZ_RELEASEID",
    ):
        value = audio.tags.get(key)

        if isinstance(value, (list, tuple)):
            value = value[0] if value else None

        if value:
            return str(value).strip()

    return None


def _rate_limit_musicbrainz():
    global _last_musicbrainz_request

    with _request_lock:
        elapsed = time.monotonic() - _last_musicbrainz_request
        delay = 1.1 - elapsed

        if delay > 0:
            time.sleep(delay)

        _last_musicbrainz_request = time.monotonic()


def _rate_limit_wikidata():
    global _last_wikidata_request

    with _request_lock:
        elapsed = time.monotonic() - _last_wikidata_request
        delay = 5.0 - elapsed

        if delay > 0:
            time.sleep(delay)

        _last_wikidata_request = time.monotonic()


def _retry_after_seconds(error: HTTPError, fallback: float) -> float:
    value = error.headers.get("Retry-After")

    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_date = parsedate_to_datetime(value)

                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(tzinfo=timezone.utc)

                return max(0.0, retry_date.timestamp() - time.time())

            except (TypeError, ValueError, OverflowError):
                pass

    return fallback


def _http_get(url: str, timeout: int = 15) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, image/*",
        },
    )

    for attempt in range(4):
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_DOWNLOAD_SIZE + 1)

            if len(data) > MAX_DOWNLOAD_SIZE:
                raise ValueError("Artwork download exceeds size limit")

            return data

        except HTTPError as error:
            if error.code != 429 or attempt == 3:
                raise

            delay = _retry_after_seconds(
                error,
                min(5.0 * (2 ** attempt), 60.0),
            )
            time.sleep(delay)

    raise RuntimeError("HTTP request retry limit exceeded")


def _download_image(url: str) -> bytes | None:
    image_data = _http_get(url)

    with Image.open(BytesIO(image_data)) as image:
        image.verify()

    return image_data


def _get_release_artist(release: dict) -> str:
    credits = release.get("artist-credit", [])

    parts = []

    for credit in credits:
        if not isinstance(credit, dict):
            continue

        artist = credit.get("artist", {})
        name = credit.get("name") or artist.get("name", "")

        parts.append(name)
        parts.append(credit.get("joinphrase", ""))

    return "".join(parts).strip()


def _search_musicbrainz(artist: str, album: str) -> str | None:
    query = f'release:"{album}" AND artist:"{artist}"'

    url = MUSICBRAINZ_URL + "?" + urlencode({
        "query": query,
        "fmt": "json",
        "limit": 5,
    })

    _rate_limit_musicbrainz()
    response = json.loads(_http_get(url))

    expected_artist = _normalize_text(artist)
    expected_album = _normalize_text(album)

    for release in response.get("releases", []):
        release_artist = _normalize_text(
            _get_release_artist(release)
        )
        release_title = _normalize_text(
            release.get("title", "")
        )

        if (
                release_artist == expected_artist
                and release_title == expected_album
        ):
            return release.get("id")

    return None


def _get_release_id(
        artist: str,
        album: str,
        explicit_release_id: str | None = None,
) -> str | None:
    if explicit_release_id:
        return explicit_release_id

    cache_key = _album_cache_key(artist, album)
    cache = _load_json_cache(LOOKUP_CACHE)
    cached = cache.get(cache_key)

    if isinstance(cached, dict):
        checked_at = cached.get("checked_at", 0)
        age = time.time() - checked_at

        if age < NEGATIVE_CACHE_DAYS * 86400:
            return cached.get("release_id")

    try:
        release_id = _search_musicbrainz(artist, album)
    except (OSError, ValueError, URLError, HTTPError):
        return None

    cache[cache_key] = {
        "release_id": release_id,
        "checked_at": time.time(),
    }

    try:
        _save_json_cache(LOOKUP_CACHE, cache)
    except OSError:
        pass

    return release_id


def _get_online_artwork(
        artist: str,
        album: str,
        release_id: str | None = None,
) -> bytes | None:
    if not artist or not album:
        return None

    if artist.casefold() == "unknown artist":
        return None

    if album.casefold() == "unknown album":
        return None

    release_id = _get_release_id(
        artist,
        album,
        release_id,
    )

    if not release_id:
        return None

    ARTWORK_CACHE.mkdir(parents=True, exist_ok=True)

    cached_path = ARTWORK_CACHE / f"{release_id}.jpg"

    if cached_path.is_file():
        try:
            return cached_path.read_bytes()
        except OSError:
            pass

    url = f"{COVER_ART_URL}{release_id}/front-500"

    try:
        artwork_data = _download_image(url)

        if not artwork_data:
            return None

        temporary_path = cached_path.with_suffix(".tmp")
        temporary_path.write_bytes(artwork_data)
        temporary_path.replace(cached_path)

        return artwork_data

    except (OSError, ValueError, URLError, HTTPError):
        return None


def _create_cached_image(
        artwork_data: bytes,
        cached_path: Path,
        size: int,
):
    with Image.open(BytesIO(artwork_data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")

    image = ImageOps.fit(
        image,
        (size, size),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    radius = max(2, int(size * 0.12))
    mask = Image.new("L", (size, size), 0)

    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=radius,
        fill=255,
    )

    image.putalpha(mask)
    image.save(cached_path, format="PNG")


def _get_cached_artwork(
        path: Path,
        size: int,
        artist: str | None = None,
        album: str | None = None,
) -> str | None:
    path = Path(path)

    try:
        stat = path.stat()

        ARTWORK_CACHE.mkdir(parents=True, exist_ok=True)

        cache_key = hashlib.sha256(
            (
                f"{path.resolve()}:{stat.st_mtime_ns}:"
                f"{stat.st_size}:{size}"
            ).encode()
        ).hexdigest()

        cached_path = ARTWORK_CACHE / f"{cache_key}.png"

        if cached_path.is_file():
            return str(cached_path)

        # 1. Embedded artwork.
        try:
            artwork_data = _get_artwork_data(path)
        except Exception:
            artwork_data = None

        # 2. Local album artwork.
        if not artwork_data:
            artwork_data = _get_local_artwork(path)

        # 3. MusicBrainz + Cover Art Archive.
        if not artwork_data and artist and album:
            try:
                release_id = _get_musicbrainz_release_id(path)
            except Exception:
                release_id = None

            artwork_data = _get_online_artwork(
                artist,
                album,
                release_id,
            )

        if not artwork_data:
            return None

        _create_cached_image(
            artwork_data,
            cached_path,
            size,
        )

        return str(cached_path)

    except Exception:
        return None


def _find_local_artist_image(artist_folder: Path) -> Path | None:
    if not artist_folder.is_dir():
        return None

    extensions = (".jpg", ".jpeg", ".png", ".webp")

    preferred_names = [
                          f"artist{extension}" for extension in extensions
                      ] + [
                          f"folder{extension}" for extension in extensions
                      ] + [
                          f"image{extension}" for extension in extensions
                      ] + [
                          f"portrait{extension}" for extension in extensions
                      ] + [
                          f"cover{extension}" for extension in extensions
                      ]

    for name in preferred_names:
        candidate = artist_folder / name

        if candidate.is_file():
            return candidate

    try:
        candidates = sorted(
            candidate
            for candidate in artist_folder.iterdir()
            if candidate.is_file()
            and candidate.suffix.casefold() in extensions
        )
    except OSError:
        return None

    return candidates[0] if candidates else None


def _search_deezer_artist(artist: str) -> bytes | None:
    url = DEEZER_URL + "/search/artist?" + urlencode({
        "q": artist,
        "limit": 10,
    })

    response = json.loads(_http_get(url))
    expected_name = _normalize_text(artist)

    for result in response.get("data", []):
        if _normalize_text(result.get("name", "")) != expected_name:
            continue

        image_url = (
                result.get("picture_xl")
                or result.get("picture_big")
                or result.get("picture_medium")
        )

        if not image_url:
            continue

        try:
            return _download_image(image_url)
        except (OSError, ValueError, URLError, HTTPError):
            continue

    return None


def _search_musicbrainz_artist(artist: str) -> str | None:
    query = f'artist:"{artist}"'

    url = MUSICBRAINZ_ARTIST_URL + "?" + urlencode({
        "query": query,
        "fmt": "json",
        "limit": 10,
    })

    _rate_limit_musicbrainz()
    response = json.loads(_http_get(url))

    expected_name = _normalize_text(artist)

    for result in response.get("artists", []):
        if _normalize_text(result.get("name", "")) != expected_name:
            continue

        artist_id = result.get("id")

        if artist_id:
            return artist_id

    return None


def _get_fanart_artist_artwork(artist: str) -> bytes | None:
    if not FANART_API_KEY:
        return None

    artist_id = _search_musicbrainz_artist(artist)

    if not artist_id:
        return None

    url = FANART_URL + quote(artist_id) + "?" + urlencode({
        "api_key": FANART_API_KEY,
    })

    try:
        response = json.loads(_http_get(url))
    except HTTPError as error:
        if error.code == 404:
            return None
        raise

    # Artist thumbnails are preferred over backgrounds and logos.
    image_types = (
        "artistthumb",
        "artistbackground",
    )

    for image_type in image_types:
        images = response.get(image_type, [])

        if not isinstance(images, list):
            continue

        # Prefer language-neutral and English images, then popularity.
        images = sorted(
            images,
            key=lambda image: (
                image.get("lang") not in ("00", "en", ""),
                -int(image.get("likes", 0) or 0),
            ),
        )

        for image in images:
            image_url = image.get("url")

            if not image_url:
                continue

            try:
                return _download_image(image_url)
            except (OSError, ValueError, URLError, HTTPError):
                continue

    return None


def _search_wikidata_artist(artist: str) -> str | None:
    url = WIKIDATA_URL + "?" + urlencode({
        "action": "wbsearchentities",
        "search": artist,
        "language": "en",
        "format": "json",
        "limit": 10,
    })

    _rate_limit_wikidata()
    response = json.loads(_http_get(url))

    expected_name = _normalize_text(artist)

    for entity in response.get("search", []):
        label = _normalize_text(entity.get("label", ""))

        if label != expected_name:
            continue

        description = _normalize_text(
            entity.get("description", "")
        )

        if description and not any(
                keyword in description
                for keyword in (
                        "musician",
                        "singer",
                        "songwriter",
                        "composer",
                        "band",
                        "musical group",
                        "music group",
                        "rapper",
                        "record producer",
                        "orchestra",
                        "duo",
                )
        ):
            continue

        entity_id = entity.get("id")

        if entity_id:
            return entity_id

    return None


def _get_wikidata_image_filename(entity_id: str) -> str | None:
    url = WIKIDATA_URL + "?" + urlencode({
        "action": "wbgetentities",
        "ids": entity_id,
        "props": "claims",
        "format": "json",
    })

    _rate_limit_wikidata()
    response = json.loads(_http_get(url))

    entity = response.get("entities", {}).get(entity_id, {})
    claims = entity.get("claims", {})
    image_claims = claims.get("P18", [])

    for claim in image_claims:
        try:
            filename = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue

        if isinstance(filename, str) and filename:
            return filename

    return None


def _get_wikidata_artist_artwork(artist: str) -> bytes | None:
    entity_id = _search_wikidata_artist(artist)

    if not entity_id:
        return None

    filename = _get_wikidata_image_filename(entity_id)

    if not filename:
        return None

    image_url = (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        f"{quote(filename.replace(' ', '_'))}?width=512"
    )

    return _download_image(image_url)


def _get_online_artist_artwork(artist: str) -> bytes | None:
    if not artist or _normalize_text(artist) in (
            "unknown artist",
            "various artists",
    ):
        return None

    cache_key = hashlib.sha256(
        _normalize_text(artist).encode()
    ).hexdigest()

    cache = _load_json_cache(ARTIST_LOOKUP_CACHE)
    cached = cache.get(cache_key)

    if isinstance(cached, dict):
        age = time.time() - cached.get("checked_at", 0)

        # Only use entries generated by the current provider chain.
        if (
                cached.get("version") == ARTIST_LOOKUP_VERSION
                and age < NEGATIVE_CACHE_DAYS * 86400
        ):
            image_path = cached.get("image_path")

            if image_path:
                cached_image = Path(image_path)

                if cached_image.is_file():
                    try:
                        return cached_image.read_bytes()
                    except OSError:
                        pass
            else:
                return None

    providers = (
        ("Deezer", _search_deezer_artist),
        ("Fanart.tv", _get_fanart_artist_artwork),
        ("Wikidata", _get_wikidata_artist_artwork),
    )

    had_error = False

    for provider_name, provider in providers:
        try:
            artwork_data = provider(artist)

            if not artwork_data:
                continue

            ARTIST_IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
            cached_path = ARTIST_IMAGE_CACHE / f"{cache_key}.image"

            temporary_path = cached_path.with_suffix(".tmp")
            temporary_path.write_bytes(artwork_data)
            temporary_path.replace(cached_path)

            cache[cache_key] = {
                "version": ARTIST_LOOKUP_VERSION,
                "provider": provider_name,
                "image_path": str(cached_path),
                "checked_at": time.time(),
            }
            _save_json_cache(ARTIST_LOOKUP_CACHE, cache)

            return artwork_data

        except HTTPError as error:
            if error.code == 404:
                continue

            had_error = True

        except (OSError, ValueError, URLError, KeyError):
            had_error = True

        except Exception:
            had_error = True

    # Only cache a negative result when all providers completed
    # without a network or other unexpected error.
    if not had_error:
        cache[cache_key] = {
            "version": ARTIST_LOOKUP_VERSION,
            "provider": None,
            "image_path": None,
            "checked_at": time.time(),
        }

        try:
            _save_json_cache(ARTIST_LOOKUP_CACHE, cache)
        except OSError:
            pass

    return None


def get_cached_artist_artwork(
        artist: str,
        artist_folder: Path,
        size: int = 256,
) -> str | None:
    """Return a square artist portrait, or None if unavailable."""
    if not artist or _normalize_text(artist) == "unknown artist":
        return None

    artist_folder = Path(artist_folder)

    try:
        local_image = _find_local_artist_image(artist_folder)

        if local_image:
            stat = local_image.stat()
            source_key = (
                f"local:{local_image.resolve()}:"
                f"{stat.st_mtime_ns}:{stat.st_size}"
            )
        else:
            source_key = f"online:{_normalize_text(artist)}"

        cache_key = hashlib.sha256(
            f"{source_key}:{size}".encode()
        ).hexdigest()

        ARTIST_IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
        cached_path = ARTIST_IMAGE_CACHE / f"{cache_key}.png"

        if cached_path.is_file():
            return str(cached_path)

        if local_image:
            try:
                artwork_data = local_image.read_bytes()
            except OSError:
                artwork_data = None
        else:
            artwork_data = None

        # Local artist-folder image has priority over online artwork.
        if not artwork_data:
            artwork_data = _get_online_artist_artwork(artist)

        if not artwork_data:
            return None

        _create_cached_image(
            artwork_data,
            cached_path,
            size,
        )

        return str(cached_path)

    except Exception:
        return None


def get_cached_artwork(
        path: Path,
        artist: str | None = None,
        album: str | None = None,
) -> str | None:
    """Return a 32x32 artwork thumbnail for the track list."""
    return _get_cached_artwork(path, 32, artist, album)


def get_cached_full_artwork(
        path: Path,
        artist: str | None = None,
        album: str | None = None,
) -> str | None:
    """Return artwork standardized to exactly 256x256 pixels."""
    return _get_cached_artwork(path, 256, artist, album)
