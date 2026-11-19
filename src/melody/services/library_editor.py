from __future__ import annotations

import shutil
from pathlib import Path

from melody.database import Database
from melody.services.artwork import _find_local_artist_image
from melody.services.artwork_editor import (
    ArtworkError,
    embed_artwork,
    prepare_image,
    write_artist_image,
    write_cover_image,
)
from melody.services.library_scanner import SUPPORTED_EXTENSIONS, _sanitize_name
from melody.services.metadata import MetadataError, TrackMetadata, write_metadata


# Images Melody reads from an album's folder (see services/artwork.py).
FOLDER_IMAGE_STEMS = {"cover", "folder", "front", "album"}
FOLDER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class LibraryEditError(Exception):
    pass


class CollectionMergeRequired(LibraryEditError):
    def __init__(self, collection_type: str, name: str):
        self.collection_type = collection_type
        self.name = name
        super().__init__(
            f"This action will merge this {collection_type} "
            f"with an existing {collection_type}: {name}"
        )


class LibraryEditor:
    def __init__(self, music_folder: Path, database: Database):
        self.music_folder = Path(music_folder).expanduser().resolve()
        self.database = database

    @staticmethod
    def _key(value: str | None) -> str:
        return str(value or "").strip().casefold()

    def _inside_library(self, path: Path) -> Path:
        path = path.expanduser().resolve()

        try:
            path.relative_to(self.music_folder)
        except ValueError:
            raise LibraryEditError(
                f"Refusing to move a file outside the music folder: {path}"
            )

        return path

    def _tracks(self):
        return self.database.get_tracks()

    def _unique_destination(self, destination: Path, reserved: set[Path]):
        destination = destination.resolve()

        if destination not in reserved and not destination.exists():
            reserved.add(destination)
            return destination

        stem = destination.stem
        suffix = destination.suffix
        parent = destination.parent
        index = 2

        while True:
            candidate = parent / f"{stem} ({index}){suffix}"
            candidate = candidate.resolve()

            if candidate not in reserved and not candidate.exists():
                reserved.add(candidate)
                return candidate

            index += 1

    def album_would_merge(
            self,
            album_artist: str,
            album: str,
            new_album: str,
            new_artist: str | None = None,
    ) -> bool:
        new_artist = (new_artist or album_artist).strip()
        new_album = new_album.strip()

        if (
                self._key(new_artist) == self._key(album_artist)
                and self._key(new_album) == self._key(album)
        ):
            return False

        return any(
            self._key(track["album_artist"] or track["artist"])
            == self._key(new_artist)
            and self._key(track["album"]) == self._key(new_album)
            and not (
                    self._key(track["album_artist"] or track["artist"])
                    == self._key(album_artist)
                    and self._key(track["album"]) == self._key(album)
            )
            for track in self._tracks()
        )

    def artist_would_merge(self, old_artist: str, new_artist: str) -> bool:
        if self._key(old_artist) == self._key(new_artist):
            return False

        return any(
            self._key(track["album_artist"] or track["artist"])
            == self._key(new_artist)
            for track in self._tracks()
        )

    def _move_collection_tracks(
            self,
            tracks: list[dict],
            metadata_factory,
            allow_merge: bool,
    ) -> dict:
        if not tracks:
            raise LibraryEditError("The selected collection no longer exists")

        # Prepare and validate every source and destination before changing files.
        operations = []
        reserved = set()

        for track in tracks:
            source = self._inside_library(Path(track["path"]))

            if not source.is_file():
                raise LibraryEditError(f"File no longer exists: {source}")

            metadata = metadata_factory(track)

            artist_folder = _sanitize_name(
                metadata.album_artist or metadata.artist
            )
            album_folder = _sanitize_name(metadata.album)
            title = _sanitize_name(metadata.title)

            destination = (
                    self.music_folder
                    / artist_folder
                    / album_folder
                    / f"{title}{source.suffix}"
            ).resolve()

            self._inside_library(destination)
            operations.append((track, source, destination, metadata))

        # Detect a merge from the actual database collection as well as the
        # filesystem. The caller is responsible for asking the user first.
        if not allow_merge:
            for _track, source, destination, _metadata in operations:
                if destination.exists() and destination != source:
                    raise CollectionMergeRequired(
                        "collection",
                        destination.parent.name,
                    )

        for index, (track, source, destination, metadata) in enumerate(operations):
            operations[index] = (
                track,
                source,
                self._unique_destination(destination, reserved)
                if destination != source else destination,
                metadata,
            )

        result = {"moved": 0, "errors": [], "merged": False}
        moves = []

        for track, source, destination, metadata in operations:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)

                write_metadata(source, metadata)

                if source != destination:
                    shutil.move(str(source), str(destination))
                else:
                    destination = source

                self.database.update_track(
                    track["id"],
                    destination,
                    metadata,
                )
                result["moved"] += 1
                moves.append((source.parent, destination.parent))

                # Remove empty folders left behind by the move, but never
                # remove the music root itself.
                parent = source.parent
                while parent != self.music_folder:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

            except Exception as error:
                result["errors"].append(f"{source}: {error}")

        self._carry_folder_images(moves, result)

        result["merged"] = bool(allow_merge)
        return result

    def rename_album(
            self,
            album_artist: str,
            album: str,
            new_album: str,
            new_artist: str | None = None,
            allow_merge: bool = False,
    ) -> dict:
        new_album = new_album.strip()
        new_artist = (new_artist or album_artist).strip()

        if not new_album:
            raise ValueError("An album name cannot be empty")
        if not new_artist:
            raise ValueError("An artist name cannot be empty")

        tracks = [
            track for track in self._tracks()
            if self._key(track["album_artist"] or track["artist"])
               == self._key(album_artist)
               and self._key(track["album"]) == self._key(album)
        ]

        if not tracks:
            raise LibraryEditError("The selected album no longer exists")

        if (
                self.album_would_merge(
                    album_artist,
                    album,
                    new_album,
                    new_artist,
                )
                and not allow_merge
        ):
            raise CollectionMergeRequired("album", new_album)

        def make_metadata(track):
            return TrackMetadata(
                title=track["title"],
                artist=track["artist"],
                album=new_album,
                album_artist=new_artist,
                duration=track["duration"],
                year=track["year"],
                genre=track["genre"],
            )

        return self._move_collection_tracks(
            tracks,
            make_metadata,
            allow_merge,
        )

    def rename_artist(
            self,
            old_artist: str,
            new_artist: str,
            allow_merge: bool = False,
    ) -> dict:
        new_artist = new_artist.strip()

        if not new_artist:
            raise ValueError("An artist name cannot be empty")

        tracks = [
            track for track in self._tracks()
            if self._key(track["album_artist"] or track["artist"])
               == self._key(old_artist)
        ]

        if not tracks:
            raise LibraryEditError("The selected artist no longer exists")

        if self.artist_would_merge(old_artist, new_artist) and not allow_merge:
            raise CollectionMergeRequired("artist", new_artist)

        def make_metadata(track):
            track_artist = track["artist"]

            if self._key(track_artist) == self._key(old_artist):
                track_artist = new_artist

            return TrackMetadata(
                title=track["title"],
                artist=track_artist,
                album=track["album"],
                album_artist=new_artist,
                duration=track["duration"],
                year=track["year"],
                genre=track["genre"],
            )

        result = self._move_collection_tracks(
            tracks,
            make_metadata,
            allow_merge,
        )

        # An artist's own picture (artist.jpg...) sits in their folder, which
        # the move has just left behind: bring it along.
        if not result["errors"]:
            self._carry_artist_image(old_artist, new_artist, result)

        return result

    def edit_track(
            self,
            track_id: str,
            title: str,
            artist: str,
            album: str,
    ) -> dict:
        """Edit one track's title (and file name), artist and album.

        The tags are rewritten and the file is moved to
        ``<album artist>/<album>/<title>.<ext>``. The track's album artist is
        left untouched.
        """
        title = title.strip()
        artist = artist.strip()
        album = album.strip()

        if not title:
            raise ValueError("A track title cannot be empty")
        if not artist:
            raise ValueError("An artist name cannot be empty")
        if not album:
            raise ValueError("An album name cannot be empty")

        track = self.database.get_track(track_id)

        if track is None:
            raise LibraryEditError("The selected track no longer exists")

        def make_metadata(current):
            return TrackMetadata(
                title=title,
                artist=artist,
                album=album,
                album_artist=current["album_artist"],
                duration=current["duration"],
                year=current["year"],
                genre=current["genre"],
            )

        try:
            return self._move_collection_tracks(
                [track],
                make_metadata,
                allow_merge=False,
            )
        except CollectionMergeRequired:
            raise LibraryEditError(
                f'A file named "{title}" already exists in that album.'
            ) from None

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def _carry_folder_images(self, moves, result: dict):
        """Move cover images along with the last file leaving a folder.

        ``moves`` is a list of ``(old folder, new folder)``. A folder's
        ``cover.jpg`` & co. follow only once no audio file is left in it, so
        moving one track out of an album never takes the album's cover away.
        """
        for old, new in set(moves):
            if old == new or not old.is_dir():
                continue

            try:
                entries = list(old.iterdir())
            except OSError:
                continue

            if any(
                    entry.is_file()
                    and entry.suffix.casefold() in SUPPORTED_EXTENSIONS
                    for entry in entries
            ):
                continue

            for entry in entries:
                if (
                        entry.is_file()
                        and entry.stem.casefold() in FOLDER_IMAGE_STEMS
                        and entry.suffix.casefold() in FOLDER_IMAGE_EXTENSIONS
                        and not (new / entry.name).exists()
                ):
                    try:
                        shutil.move(str(entry), str(new / entry.name))
                    except OSError as error:
                        result["errors"].append(f"{entry}: {error}")

            # Remove what the move left empty, never the music root itself.
            parent = old
            while parent != self.music_folder:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _carry_artist_image(self, old_artist: str, new_artist: str, result: dict):
        old_folder = self.music_folder / _sanitize_name(old_artist)
        new_folder = self.music_folder / _sanitize_name(new_artist)

        if old_folder == new_folder or not old_folder.is_dir():
            return

        image = _find_local_artist_image(old_folder)

        # Never replace a picture the destination artist already has.
        if image is None or _find_local_artist_image(new_folder) is not None:
            return

        try:
            new_folder.mkdir(parents=True, exist_ok=True)
            shutil.move(
                str(image),
                str(new_folder / f"artist{image.suffix.lower()}"),
            )

            try:
                old_folder.rmdir()
            except OSError:
                pass

        except OSError as error:
            result["errors"].append(f"{image}: {error}")

    def _embed_cover(self, tracks: list[dict], data: bytes) -> dict:
        """Embed ``data`` as the cover of every track in ``tracks``."""
        try:
            image, mime = prepare_image(data)
        except ArtworkError as error:
            raise LibraryEditError(str(error)) from None

        result = {"updated": 0, "errors": []}

        for track in tracks:
            try:
                source = self._inside_library(Path(track["path"]))

                embed_artwork(source, image, mime)

                # The file changed on disk: record its new size and date so
                # the next library scan doesn't see it as modified.
                self.database.update_track(
                    track["id"],
                    source,
                    TrackMetadata(
                        title=track["title"],
                        artist=track["artist"],
                        album=track["album"],
                        album_artist=track["album_artist"],
                        duration=track["duration"],
                        year=track["year"],
                        genre=track["genre"],
                    ),
                )
                result["updated"] += 1

            except Exception as error:
                result["errors"].append(f"{track['path']}: {error}")

        return result

    def set_track_image(self, track_id: str, data: bytes) -> dict:
        """Embed ``data`` as the cover of one track's file."""
        track = self.database.get_track(track_id)

        if track is None:
            raise LibraryEditError("The selected track no longer exists")

        return self._embed_cover([track], data)

    def set_album_image(
            self,
            album_artist: str,
            album: str,
            data: bytes,
            *,
            apply_to_tracks: bool = True,
            write_folder: bool = True,
    ) -> dict:
        """Set an album's artwork.

        ``write_folder`` saves it as ``cover.jpg`` in the album's folder,
        which is the album's own cover. ``apply_to_tracks`` also embeds it in
        every track's file.
        """
        tracks = [
            track for track in self._tracks()
            if self._key(track["album_artist"] or track["artist"])
               == self._key(album_artist)
               and self._key(track["album"]) == self._key(album)
        ]

        if not tracks:
            raise LibraryEditError("The selected album no longer exists")

        result = {"updated": 0, "errors": []}

        if apply_to_tracks:
            result = self._embed_cover(tracks, data)

        if not write_folder:
            return result

        # The album's own cover: a cover.jpg in its folder, where Melody (and
        # other players) look for it.
        try:
            image, _mime = prepare_image(data, jpeg_only=True)
        except ArtworkError as error:
            raise LibraryEditError(str(error)) from None

        for folder in sorted({Path(track["path"]).parent for track in tracks}):
            try:
                write_cover_image(self._inside_library(folder), image)
            except Exception as error:
                result["errors"].append(f"{folder}: {error}")

        return result

    def set_artist_image(self, artist: str, data: bytes) -> dict:
        """Save ``data`` as ``artist.jpg`` in the artist's folder."""
        artist = artist.strip()

        if not any(
                self._key(track["album_artist"] or track["artist"])
                == self._key(artist)
                for track in self._tracks()
        ):
            raise LibraryEditError("The selected artist no longer exists")

        try:
            image, _mime = prepare_image(data, jpeg_only=True)
        except ArtworkError as error:
            raise LibraryEditError(str(error)) from None

        folder = self._inside_library(self.music_folder / _sanitize_name(artist))

        if not folder.is_dir():
            raise LibraryEditError(f"The artist's folder does not exist: {folder}")

        try:
            write_artist_image(folder, image)
        except ArtworkError as error:
            raise LibraryEditError(str(error)) from None

        return {"updated": 1, "errors": []}
