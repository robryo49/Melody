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
from melody.services.replaygain import ReplayGainError, write_track_gain


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
            title = _sanitize_name(metadata.title)

            # No album means a single: it goes straight in the artist's
            # folder, with no album folder.
            if metadata.album.strip():
                folder = (
                    self.music_folder
                    / artist_folder
                    / _sanitize_name(metadata.album)
                )
            else:
                folder = self.music_folder / artist_folder

            destination = (folder / f"{title}{source.suffix}").resolve()

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
        ``<album artist>/<album>/<title>.<ext>``, or to
        ``<album artist>/<title>.<ext>`` when the album is empty (a single).
        The track's album artist is left untouched.
        """
        title = title.strip()
        artist = artist.strip()
        album = album.strip()

        if not title:
            raise ValueError("A track title cannot be empty")
        if not artist:
            raise ValueError("An artist name cannot be empty")

        track = self.database.get_track(track_id)

        if track is None:
            raise LibraryEditError("The selected track no longer exists")

        def make_metadata(current):
            # A track's album artist always matches its artist, so changing the
            # artist also changes the album artist: the file is then filed
            # under the new artist, in their own album.
            return TrackMetadata(
                title=title,
                artist=artist,
                album=album,
                album_artist=artist,
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

    def edit_tracks(
            self,
            track_ids,
            *,
            artist: str | None = None,
            album: str | None = None,
            album_artist: str | None = None,
    ) -> dict:
        """Change the artist and / or the album of several tracks at once.

        A tag given as ``None`` is left as each track has it. Giving an
        ``artist`` without an ``album_artist`` also makes it the tracks' album
        artist, as editing one track does (see ``edit_track``). Titles are
        never touched.

        The tags are rewritten and the files moved to
        ``<album artist>/<album>/<title>.<ext>``, or to
        ``<album artist>/<title>.<ext>`` for a track with no album. A file
        that would land on another one gets a number, so nothing is ever
        overwritten, and a track that already has what is asked is left
        alone.

        Returns what the move returns: ``{"moved": how many tracks were
        changed, "errors": [...], ...}``. Nothing is changed when one of the
        tracks no longer exists.
        """
        artist = None if artist is None else artist.strip()
        album = None if album is None else album.strip()
        album_artist = None if album_artist is None else album_artist.strip()

        if artist is not None and not artist:
            raise ValueError("An artist name cannot be empty")
        if album is not None and not album:
            raise ValueError("An album name cannot be empty")
        if album_artist is not None and not album_artist:
            raise ValueError("An album artist name cannot be empty")

        def wanted(track):
            """The (artist, album, album artist) ``track`` should end up
            with."""
            if album_artist is not None:
                new_album_artist = album_artist
            elif artist is not None:
                new_album_artist = artist
            else:
                new_album_artist = track["album_artist"]

            return (
                track["artist"] if artist is None else artist,
                track["album"] if album is None else album,
                new_album_artist,
            )

        tracks = []

        for track_id in dict.fromkeys(track_ids):
            track = self.database.get_track(track_id)

            if track is None:
                raise LibraryEditError("A selected track no longer exists")

            current = (
                track["artist"],
                track["album"],
                track["album_artist"],
            )

            if wanted(track) != current:
                tracks.append(track)

        if not tracks:
            return {"moved": 0, "errors": [], "merged": False}

        def make_metadata(current):
            new_artist, new_album, new_album_artist = wanted(current)

            return TrackMetadata(
                title=current["title"],
                artist=new_artist,
                album=new_album,
                album_artist=new_album_artist,
                duration=current["duration"],
                year=current["year"],
                genre=current["genre"],
            )

        # Tracks moving into an album that exists are not a merge to ask
        # about: a name that is taken just gets a number.
        return self._move_collection_tracks(
            tracks,
            make_metadata,
            allow_merge=True,
        )

    def import_track(
            self,
            source: Path,
            metadata: TrackMetadata,
            *,
            image: bytes | None = None,
            start_time: float | None = None,
            end_time: float | None = None,
            kind: str | None = None,
            replaygain: float | None = None,
            replaygain_peak: float | None = None,
    ) -> dict:
        """Add an audio file to the library as a new track.

        The file is *copied* (the original is left where it is) to
        ``<album artist>/<album>/<title>.<ext>``, or to
        ``<album artist>/<title>.<ext>`` when the album is empty (a single),
        with its tags rewritten from ``metadata``. If a file with that name is
        already there, the copy gets a number, so nothing is ever overwritten.

        ``image`` (raw bytes) is embedded as the track's cover, and
        ``start_time`` / ``end_time`` / ``kind`` are stored like the edit
        window does. ``replaygain`` (dB, with the track's ``replaygain_peak``)
        is written into the copy's tags.

        Returns ``{"id": the new track's id, "errors": [...]}``; the errors
        are about the extras (cover, range, kind), which the track can do
        without. If the file itself cannot be added, ``LibraryEditError`` is
        raised and nothing is left behind.
        """
        source = Path(source).expanduser().resolve()

        if not source.is_file():
            raise LibraryEditError(f"{source.name}: the file no longer exists")

        if source.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            raise LibraryEditError(
                f"{source.name}: not a supported audio file"
            )

        try:
            source.relative_to(self.music_folder)
        except ValueError:
            pass
        else:
            raise LibraryEditError(
                f"{source.name}: this file is already in the library"
            )

        folder = self.music_folder / _sanitize_name(
            metadata.album_artist or metadata.artist
        )

        if metadata.album.strip():
            folder = folder / _sanitize_name(metadata.album)

        destination = self._unique_destination(
            folder / f"{_sanitize_name(metadata.title)}{source.suffix}",
            set(),
        )
        self._inside_library(destination)

        # Problems with the ReplayGain, which the import can do without.
        gain_errors = []

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            write_metadata(destination, metadata)

            # Written before the file is measured below, so the library
            # records the file as it ends up. The track can do without it:
            # a failure is reported, it does not undo the import.
            if replaygain is not None:
                try:
                    write_track_gain(destination, replaygain, replaygain_peak)
                except ReplayGainError as error:
                    gain_errors.append(
                        f"{source.name}: ReplayGain: {error}"
                    )

            stat = destination.stat()
            self.database.save_track(
                path=destination,
                metadata=metadata,
                file_size=stat.st_size,
                file_mtime=stat.st_mtime,
            )

            track = self.database.get_track_by_path(destination)

            if track is None:
                raise LibraryEditError("it could not be saved to the library")

        except Exception as error:
            self._discard_import(destination)
            raise LibraryEditError(f"{source.name}: {error}") from None

        track_id = track["id"]
        result = {"id": track_id, "errors": list(gain_errors)}

        if image:
            try:
                result["errors"].extend(
                    self.set_track_image(track_id, image)["errors"]
                )
            except Exception as error:
                result["errors"].append(f"{source.name}: cover: {error}")

        try:
            if start_time is not None or end_time is not None:
                self.database.set_track_range(track_id, start_time, end_time)

            if kind is not None:
                self.database.set_track_kind(track_id, kind)
        except Exception as error:
            result["errors"].append(f"{source.name}: {error}")

        return result

    def _discard_import(self, destination: Path):
        """Undo a copy that failed half way: the file, and the folders it
        created, but never the music root itself."""
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass

        parent = destination.parent

        while parent != self.music_folder:
            try:
                parent.rmdir()
            except OSError:
                break

            parent = parent.parent

    def delete_track(self, track_id: str) -> dict:
        """Delete a track: its file is moved to the trash, and it leaves the
        library and every playlist it was in.

        Returns ``{"errors": [...]}`` for the things around the track that
        could not be tidied up. If the file itself cannot be moved to the
        trash, ``LibraryEditError`` is raised and nothing is deleted.
        """
        track = self.database.get_track(track_id)

        if track is None:
            raise LibraryEditError("The selected track no longer exists")

        path = self._inside_library(Path(track["path"]))
        result = {"errors": []}

        if path.exists():
            self._trash(path)

        self.database.delete_track(track_id)
        self._remove_emptied_folder(path.parent, result)

        return result

    @staticmethod
    def _trash(path: Path):
        from gi.repository import Gio, GLib

        try:
            Gio.File.new_for_path(str(path)).trash(None)
        except GLib.Error as error:
            raise LibraryEditError(
                f"Could not move {path.name} to the trash: {error.message}"
            ) from None

    def _remove_emptied_folder(self, folder: Path, result: dict):
        """Tidy the folder a deleted track was in.

        An album folder left without any audio only holds its cover image,
        which goes too. An artist's folder is never emptied that way: it
        holds the artist's own picture. Then every folder left empty is
        removed, up to (never including) the music folder.
        """
        if (
                folder != self.music_folder
                and folder.parent != self.music_folder
                and folder.is_dir()
        ):
            try:
                entries = list(folder.iterdir())
            except OSError:
                entries = []

            if not any(
                    entry.is_file()
                    and entry.suffix.casefold() in SUPPORTED_EXTENSIONS
                    for entry in entries
            ):
                for entry in entries:
                    if (
                            entry.is_file()
                            and entry.stem.casefold() in FOLDER_IMAGE_STEMS
                            and entry.suffix.casefold()
                            in FOLDER_IMAGE_EXTENSIONS
                    ):
                        try:
                            self._trash(entry)
                        except LibraryEditError as error:
                            result["errors"].append(str(error))

        parent = folder

        while parent != self.music_folder:
            try:
                parent.rmdir()
            except OSError:
                break

            parent = parent.parent

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

            # An artist's folder holds the artist's own picture (and their
            # singles), never an album cover: leave it where it is.
            if old.parent == self.music_folder:
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
