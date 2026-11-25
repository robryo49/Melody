
from dataclasses import dataclass, field
from pathlib import Path

from melody.database import Database
from melody.services.library_scanner import scan_library
from melody.services.organizer import organize_file, OrganizationError
from melody.services.settings import default_music_folder


@dataclass
class SyncReport:
    discovered: int = 0
    added: int = 0
    moved: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


class LibraryManager:
    def __init__(
            self,
            music_folder: Path | None = None,
            database: Database | None = None,
    ):
        self.music_folder = Path(
            music_folder or default_music_folder()
        ).expanduser().resolve()

        self.database = database or Database()

    def sync(self, organize: bool = True) -> SyncReport:
        """Bring the database up to date with the music folder.

        With ``organize``, every file that is not where its tags say it
        belongs (``Artist/Album/Title``) is moved there. Without it the files
        stay exactly where they are.
        """
        report = SyncReport()

        if not self.music_folder.is_dir():
            raise NotADirectoryError(
                f"Music folder does not exist: {self.music_folder}"
            )

        scan = scan_library(self.music_folder)
        report.discovered = len(scan.entries)

        report.errors.extend(
            f"{error.path}: {error.message}"
            for error in scan.errors
        )

        # Preserve existing database entries for files that could not be read.
        existing_paths = {
            error.path.resolve()
            for error in scan.errors
            if error.path.is_file()
        }

        for entry in scan.entries:
            original_path = entry.path
            final_path = original_path

            try:
                if organize:
                    final_path = organize_file(entry, self.music_folder)

            except OrganizationError as error:
                report.errors.append(str(error))

            try:
                stat = final_path.stat()

                result = self.database.save_track(
                    path=final_path,
                    metadata=entry.metadata,
                    file_size=stat.st_size,
                    file_mtime=stat.st_mtime,
                    previous_path=original_path,
                )

                existing_paths.add(final_path.resolve())

                if final_path != original_path:
                    report.moved += 1

                if result == "added":
                    report.added += 1
                elif result == "updated":
                    report.updated += 1
                else:
                    report.unchanged += 1

            except Exception as error:
                if final_path.is_file():
                    existing_paths.add(final_path.resolve())

                report.errors.append(
                    f"Could not register {final_path}: {error}"
                )

        report.removed = self.database.remove_missing(existing_paths)

        return report
