"""Melody's settings, kept in a small JSON file next to the database."""

import json
import math
import os
from pathlib import Path

DEFAULTS = {
    # Tell compilations (long audio files) apart from single tracks.
    "compilations_enabled": False,
    # Audio longer than this many seconds is a compilation (15:00).
    "compilation_threshold": 900.0,
    # Also list compilations in the album their file belongs to.
    "show_compilations_in_albums": False,
    # What the Recent page shows: the N most recently added tracks. Set in
    # the page's own header.
    "recent_count": 30.0,
    # What the Most played page shows: the N most played tracks. Also set in
    # the page's own header.
    "most_played_count": 30.0,
}


def default_settings_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "melody" / "settings.json"


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_settings_path()
        self._values = dict(DEFAULTS)
        self._callbacks = []
        self._load()

    # -- storage ---------------------------------------------------------

    @staticmethod
    def _coerce(key, value):
        """``value`` as the right type for ``key``; ValueError if it can't be."""
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            return value

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")

        value = float(value)

        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive number")

        return value

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        # Older versions could show the tracks added in the last N days
        # instead. While that was on, the saved count was never used and is
        # just the old default: start from the new default instead.
        if data.get("recent_by_days") is True:
            data.pop("recent_count", None)

        for key in DEFAULTS:
            if key in data:
                try:
                    self._values[key] = self._coerce(key, data[key])
                except ValueError:
                    pass

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self._values, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as error:
            print(f"Could not save the settings: {error}")

    # -- access ----------------------------------------------------------

    def connect_changed(self, callback):
        """``callback()`` runs whenever a setting changes."""
        self._callbacks.append(callback)

    def get(self, key):
        return self._values[key]

    def set(self, key, value):
        value = self._coerce(key, value)

        if self._values[key] == value:
            return

        self._values[key] = value
        self._save()

        for callback in list(self._callbacks):
            callback()

    @property
    def compilations_enabled(self) -> bool:
        return self._values["compilations_enabled"]

    @compilations_enabled.setter
    def compilations_enabled(self, value):
        self.set("compilations_enabled", value)

    @property
    def compilation_threshold(self) -> float:
        return self._values["compilation_threshold"]

    @compilation_threshold.setter
    def compilation_threshold(self, value):
        self.set("compilation_threshold", value)

    @property
    def show_compilations_in_albums(self) -> bool:
        return self._values["show_compilations_in_albums"]

    @show_compilations_in_albums.setter
    def show_compilations_in_albums(self, value):
        self.set("show_compilations_in_albums", value)

    @property
    def recent_count(self) -> float:
        return self._values["recent_count"]

    @recent_count.setter
    def recent_count(self, value):
        self.set("recent_count", value)

    @property
    def most_played_count(self) -> float:
        return self._values["most_played_count"]

    @most_played_count.setter
    def most_played_count(self, value):
        self.set("most_played_count", value)


_settings = None


def get_settings() -> Settings:
    """The one ``Settings`` shared by the whole app."""
    global _settings

    if _settings is None:
        _settings = Settings()

    return _settings
