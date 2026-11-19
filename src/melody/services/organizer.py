
import re
import shutil
from pathlib import Path

from melody.services.library_scanner import ScanEntry


class OrganizationError(Exception):
    pass


def _is_existing_collision_path(source: Path, destination: Path) -> bool:
    if source.parent != destination.parent or source.suffix != destination.suffix:
        return False

    pattern = re.escape(destination.stem) + r" \(\d+\)"
    return re.fullmatch(pattern, source.stem) is not None


def _get_unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination

    counter = 2

    while True:
        candidate = destination.with_name(
            f"{destination.stem} ({counter}){destination.suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


def organize_file(entry: ScanEntry, root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    source = entry.path.resolve()
    destination = entry.destination.resolve()

    try:
        source.relative_to(root)
        destination.relative_to(root)
    except ValueError as error:
        raise OrganizationError(
            "Source and destination must remain inside the music folder."
        ) from error

    if not source.is_file():
        raise OrganizationError(f"Source file does not exist: {source}")

    # Keep an existing numbered filename if it already represents this
    # track's destination. This prevents suffixes increasing on every scan.
    if source == destination or _is_existing_collision_path(source, destination):
        return source

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _get_unique_destination(destination)

    try:
        with source.open("rb") as source_file:
            with destination.open("xb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)

        shutil.copystat(source, destination)
        source.unlink()

    except Exception as error:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass

        raise OrganizationError(
            f"Could not organize {source}: {error}"
        ) from error

    # Remove the folders the move left empty (an old "Unknown Album" folder,
    # say), but never the music root itself.
    parent = source.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent

    return destination


def organize_library(entries: list[ScanEntry], root: Path):
    results = []
    errors = []

    for entry in entries:
        try:
            destination = organize_file(entry, root)
            results.append((entry, destination))
        except OrganizationError as error:
            errors.append((entry.path, str(error)))

    return results, errors
