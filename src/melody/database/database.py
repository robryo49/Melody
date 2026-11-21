from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from melody.services.metadata import TrackMetadata


def get_default_database_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "melody" / "melody.db"


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else get_default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self._connect() as connection:
            connection.execute("""
                               CREATE TABLE IF NOT EXISTS tracks (
                                                                     id TEXT PRIMARY KEY,
                                                                     path TEXT NOT NULL UNIQUE,
                                                                     title TEXT NOT NULL,
                                                                     artist TEXT NOT NULL,
                                                                     album TEXT NOT NULL,
                                                                     album_artist TEXT NOT NULL,
                                                                     duration REAL NOT NULL DEFAULT 0,
                                                                     year TEXT,
                                                                     genre TEXT,
                                                                     file_size INTEGER,
                                                                     file_mtime REAL,
                                                                     added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                                                     modified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                               )
                               """)

            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tracks)")
            }

            for name, definition in {
                "file_size": "INTEGER",
                "file_mtime": "REAL",
                # Where the track starts / stops playing, in seconds. NULL
                # means the very start / the very end of the file.
                "start_time": "REAL",
                "end_time": "REAL",
                # NULL: single track or compilation by length (see
                # services/compilations.py). 'track' / 'compilation' forces it.
                "kind": "TEXT",
                # How many times the track was played (see
                # PlaybackManager.on_play_counted).
                "play_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE tracks ADD COLUMN {name} {definition}"
                    )

            connection.execute("""
                               CREATE INDEX IF NOT EXISTS idx_tracks_artist
                                   ON tracks(artist)
                               """)
            connection.execute("""
                               CREATE INDEX IF NOT EXISTS idx_tracks_album
                                   ON tracks(album)
                               """)

            connection.execute("""
                               CREATE TABLE IF NOT EXISTS playlists (
                                                                        id TEXT PRIMARY KEY,
                                                                        name TEXT NOT NULL,
                                                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                                                        modified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                               )
                               """)

            connection.execute("""
                               CREATE TABLE IF NOT EXISTS playlist_tracks (
                                                                              playlist_id TEXT NOT NULL,
                                                                              track_id TEXT NOT NULL,
                                                                              position INTEGER NOT NULL DEFAULT 0,
                                                                              added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                                                              PRIMARY KEY (playlist_id, track_id),
                                   FOREIGN KEY (playlist_id) REFERENCES playlists(id)
                                   ON DELETE CASCADE,
                                   FOREIGN KEY (track_id) REFERENCES tracks(id)
                                   ON DELETE CASCADE
                                   )
                               """)

            connection.execute("""
                               CREATE INDEX IF NOT EXISTS idx_playlist_tracks_position
                                   ON playlist_tracks(playlist_id, position)
                               """)

            connection.execute("""
                               CREATE INDEX IF NOT EXISTS idx_tracks_album_collection
                                   ON tracks(album_artist, album)
                               """)

    @staticmethod
    def _metadata_values(metadata):
        return (
            metadata.title,
            metadata.artist,
            metadata.album,
            metadata.album_artist,
            metadata.duration,
            metadata.year,
            metadata.genre,
        )

    def save_track(
            self,
            path,
            metadata,
            file_size,
            file_mtime,
            previous_path=None,
    ):
        path = Path(path).expanduser().resolve()
        path_string = str(path)

        previous_path_string = (
            str(Path(previous_path).expanduser().resolve())
            if previous_path is not None else None
        )

        metadata_values = self._metadata_values(metadata)

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracks WHERE path = ?",
                (path_string,),
            ).fetchone()

            if row is None and previous_path_string:
                row = connection.execute(
                    "SELECT * FROM tracks WHERE path = ?",
                    (previous_path_string,),
                ).fetchone()

            values = (
                path_string,
                *metadata_values,
                file_size,
                file_mtime,
            )

            if row is None:
                connection.execute("""
                                   INSERT INTO tracks (
                                       id, path, title, artist, album, album_artist,
                                       duration, year, genre, file_size, file_mtime
                                   )
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                   """, (str(uuid.uuid4()), *values))
                return "added"

            old_values = (
                row["path"],
                row["title"],
                row["artist"],
                row["album"],
                row["album_artist"],
                row["duration"],
                row["year"],
                row["genre"],
                row["file_size"],
                row["file_mtime"],
            )

            if old_values == values:
                return "unchanged"

            connection.execute("""
                               UPDATE tracks
                               SET path = ?, title = ?, artist = ?, album = ?,
                                   album_artist = ?, duration = ?, year = ?, genre = ?,
                                   file_size = ?, file_mtime = ?,
                                   modified_at = CURRENT_TIMESTAMP
                               WHERE id = ?
                               """, (*values, row["id"]))

            return "updated"

    def remove_missing(self, existing_paths: set[Path]) -> int:
        existing = {
            str(Path(path).expanduser().resolve())
            for path in existing_paths
        }

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, path FROM tracks"
            ).fetchall()

            missing_ids = [
                row["id"] for row in rows
                if row["path"] not in existing
            ]

            if missing_ids:
                connection.executemany(
                    "DELETE FROM tracks WHERE id = ?",
                    [(track_id,) for track_id in missing_ids],
                )

        return len(missing_ids)

    def get_tracks(self):
        with self._connect() as connection:
            rows = connection.execute("""
                                      SELECT *
                                      FROM tracks
                                      ORDER BY artist COLLATE NOCASE,
                                               album COLLATE NOCASE,
                                               title COLLATE NOCASE
                                      """).fetchall()

        return [dict(row) for row in rows]

    def get_track(self, track_id: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracks WHERE id = ?",
                (track_id,),
            ).fetchone()

        return dict(row) if row else None

    def delete_track(self, track_id: str):
        """Remove a track from the library. Its place in every playlist goes
        with it (the playlists themselves stay)."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM tracks WHERE id = ?",
                (track_id,),
            )

    def get_track_by_path(self, path: Path):
        path = str(Path(path).expanduser().resolve())

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracks WHERE path = ?",
                (path,),
            ).fetchone()

        return dict(row) if row else None

    def get_track_count(self):
        with self._connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM tracks"
            ).fetchone()[0]

    def set_track_range(
            self,
            track_id: str,
            start_time: float | None,
            end_time: float | None,
    ):
        """Remember where a track starts and stops playing.

        ``None`` for either means the very start / end of the file. Library
        scans and edits never touch these two columns, so they survive both.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tracks SET start_time = ?, end_time = ? WHERE id = ?",
                (start_time, end_time, track_id),
            )

            if cursor.rowcount == 0:
                raise KeyError(f"Track does not exist: {track_id}")

    def set_track_kind(self, track_id: str, kind: str | None):
        """Force a track to be a 'track' or a 'compilation' (``None`` goes
        back to deciding by its length)."""
        if kind not in (None, "track", "compilation"):
            raise ValueError(f"Invalid track kind: {kind}")

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tracks SET kind = ? WHERE id = ?",
                (kind, track_id),
            )

            if cursor.rowcount == 0:
                raise KeyError(f"Track does not exist: {track_id}")

    def increment_play_count(self, track_id: str) -> int:
        """Count one more play of a track. Returns the new count.

        Library scans and edits never touch this column, so it survives both.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tracks SET play_count = play_count + 1 WHERE id = ?",
                (track_id,),
            )

            if cursor.rowcount == 0:
                raise KeyError(f"Track does not exist: {track_id}")

            return connection.execute(
                "SELECT play_count FROM tracks WHERE id = ?",
                (track_id,),
            ).fetchone()[0]

    def update_track(self, track_id: str, path: Path, metadata):
        path = str(Path(path).expanduser().resolve())

        with self._connect() as connection:
            connection.execute("""
                               UPDATE tracks
                               SET path = ?, title = ?, artist = ?, album = ?,
                                   album_artist = ?, duration = ?, year = ?, genre = ?,
                                   file_size = ?, file_mtime = ?,
                                   modified_at = CURRENT_TIMESTAMP
                               WHERE id = ?
                               """, (
                                   path,
                                   metadata.title,
                                   metadata.artist,
                                   metadata.album,
                                   metadata.album_artist,
                                   metadata.duration,
                                   metadata.year,
                                   metadata.genre,
                                   Path(path).stat().st_size,
                                   Path(path).stat().st_mtime,
                                   track_id,
                               ))

    # Playlists

    def create_playlist(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("A playlist name cannot be empty")

        playlist_id = str(uuid.uuid4())

        with self._connect() as connection:
            connection.execute(
                "INSERT INTO playlists (id, name) VALUES (?, ?)",
                (playlist_id, name),
            )

        return playlist_id

    def rename_playlist(self, playlist_id: str, name: str):
        name = name.strip()
        if not name:
            raise ValueError("A playlist name cannot be empty")

        with self._connect() as connection:
            cursor = connection.execute("""
                                        UPDATE playlists
                                        SET name = ?, modified_at = CURRENT_TIMESTAMP
                                        WHERE id = ?
                                        """, (name, playlist_id))

            if cursor.rowcount == 0:
                raise KeyError(f"Playlist does not exist: {playlist_id}")

    def delete_playlist(self, playlist_id: str):
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM playlists WHERE id = ?",
                (playlist_id,),
            )

        # The picture chosen for the playlist, if any, goes with it.
        try:
            self.playlist_cover_path(playlist_id).unlink()
        except OSError:
            pass

    def playlist_cover_path(self, playlist_id: str) -> Path:
        """Where the picture chosen for a playlist is kept (the file only
        exists once a picture has been chosen)."""
        return self.path.parent / "playlist-covers" / f"{playlist_id}.png"

    def get_playlists(self):
        with self._connect() as connection:
            rows = connection.execute("""
                                      SELECT p.*, COUNT(pt.track_id) AS track_count
                                      FROM playlists p
                                               LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
                                      GROUP BY p.id
                                      ORDER BY p.name COLLATE NOCASE
                                      """).fetchall()

        return [dict(row) for row in rows]

    def get_playlist(self, playlist_id: str):
        with self._connect() as connection:
            row = connection.execute("""
                                     SELECT p.*, COUNT(pt.track_id) AS track_count
                                     FROM playlists p
                                              LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
                                     WHERE p.id = ?
                                     GROUP BY p.id
                                     """, (playlist_id,)).fetchone()

        return dict(row) if row else None

    def add_track_to_playlist(self, playlist_id: str, track_id: str):
        with self._connect() as connection:
            position = connection.execute("""
                                          SELECT COALESCE(MAX(position), -1) + 1
                                          FROM playlist_tracks
                                          WHERE playlist_id = ?
                                          """, (playlist_id,)).fetchone()[0]

            connection.execute("""
                               INSERT OR IGNORE INTO playlist_tracks (
                    playlist_id, track_id, position
                )
                VALUES (?, ?, ?)
                               """, (playlist_id, track_id, position))

            connection.execute("""
                               UPDATE playlists
                               SET modified_at = CURRENT_TIMESTAMP
                               WHERE id = ?
                               """, (playlist_id,))

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]):
        for track_id in track_ids:
            self.add_track_to_playlist(playlist_id, track_id)

    def remove_track_from_playlist(self, playlist_id: str, track_id: str):
        with self._connect() as connection:
            connection.execute("""
                               DELETE FROM playlist_tracks
                               WHERE playlist_id = ? AND track_id = ?
                               """, (playlist_id, track_id))

            connection.execute("""
                               UPDATE playlists
                               SET modified_at = CURRENT_TIMESTAMP
                               WHERE id = ?
                               """, (playlist_id,))

    def get_playlist_tracks(self, playlist_id: str):
        with self._connect() as connection:
            rows = connection.execute("""
                                      SELECT t.*
                                      FROM tracks t
                                               JOIN playlist_tracks pt ON pt.track_id = t.id
                                      WHERE pt.playlist_id = ?
                                      ORDER BY pt.position, t.title COLLATE NOCASE
                                      """, (playlist_id,)).fetchall()

        return [dict(row) for row in rows]