"""Melody's session: what was on screen and playing when it was closed, to
pick it up again on the next start.

It is a small JSON file next to the settings. The window fills it in when it
closes and reads it back once the library is loaded (see
``MelodyWindow._restore_session``).
"""

import json
import os
from pathlib import Path


def default_session_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "melody" / "session.json"


def load_session(path: Path | None = None) -> dict | None:
    """The saved session, or None when there is none or it cannot be read."""
    path = Path(path) if path else default_session_path()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def save_session(session: dict, path: Path | None = None) -> None:
    path = Path(path) if path else default_session_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(session, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        print(f"Could not save the session: {error}")
