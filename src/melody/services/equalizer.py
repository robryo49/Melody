"""The sound options: the equalizer (ten bands, each boosted or cut by a number
of decibels) and whether ReplayGain is on.

This holds what the person chose and keeps it between launches. It knows
nothing about audio: ``connect_changed`` is how the player is told (see
``PlaybackManager.set_equalizer`` and ``set_replaygain_enabled``).

It is saved in a file of its own rather than in ``settings.py``: every change
there makes every page build its list again, which would happen for every
step of a slider that is being dragged.
"""

import json
import math

from gi.repository import GLib

from melody.services.settings import default_settings_path

BAND_COUNT = 10

# What each band is centered on (Hz), as written under its slider.
BAND_LABELS = (
    "30", "60", "120", "240", "470", "950", "1.9k", "3.8k", "7.5k", "15k",
)

# The range of the sliders, in dB.
MIN_GAIN = -12.0
MAX_GAIN = 12.0
GAIN_STEP = 0.5

PRESETS = {
    "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Bass boost": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "Bass reduce": [-6, -5, -4, -2, 0, 0, 0, 0, 0, 0],
    "Treble boost": [0, 0, 0, 0, 0, 1, 3, 5, 6, 6],
    "Vocal": [-2, -2, -1, 1, 3, 4, 3, 1, 0, -1],
    "Rock": [5, 4, 2, -1, -2, -1, 2, 4, 5, 5],
    "Pop": [-1, 1, 3, 4, 3, 0, -1, -1, 1, 2],
    "Jazz": [3, 2, 1, 2, -2, -2, 0, 1, 2, 3],
    "Classical": [0, 0, 0, 0, 0, 0, -3, -3, -3, -4],
    "Electronic": [5, 4, 1, 0, -2, 2, 1, 1, 4, 5],
}

CUSTOM = "Custom"

# A slider being dragged changes the gains many times a second: the file is
# only written once it has stopped for this long (milliseconds).
SAVE_DELAY = 500


def equalizer_path():
    return default_settings_path().with_name("equalizer.json")


def _clean_gain(value):
    """``value`` as a gain: a number of dB within the sliders' range, in
    steps of ``GAIN_STEP``."""
    value = float(value)

    if not math.isfinite(value):
        raise ValueError("a gain must be a number")

    value = round(value / GAIN_STEP) * GAIN_STEP
    return max(MIN_GAIN, min(MAX_GAIN, value))


class Equalizer:
    def __init__(self, path=None):
        self.path = path or equalizer_path()
        self.enabled = False
        self.gains = [0.0] * BAND_COUNT
        # Whether tracks are played with their ReplayGain tag (and the edit
        # window shows that tag).
        self.replaygain_enabled = False
        self._callbacks = []
        self._save_source = None
        self._load()

    # -- storage ---------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.replaygain_enabled = bool(data.get("replaygain", False))
            enabled = bool(data.get("enabled", False))
            gains = [_clean_gain(value) for value in data["gains"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return

        if len(gains) != BAND_COUNT:
            return

        self.enabled = enabled
        self.gains = gains

    def flush(self):
        """Write what has not been saved yet (done when the app closes)."""
        if self._save_source is not None:
            GLib.source_remove(self._save_source)
            self._save_source = None

        self._save()

    def _save(self):
        self._save_source = None

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "enabled": self.enabled,
                        "gains": self.gains,
                        "replaygain": self.replaygain_enabled,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as error:
            print(f"Could not save the equalizer: {error}")

        return GLib.SOURCE_REMOVE

    # -- access ----------------------------------------------------------

    def connect_changed(self, callback):
        """``callback()`` runs whenever the equalizer changes."""
        self._callbacks.append(callback)

    def disconnect_changed(self, callback):
        """Stop calling ``callback()`` (see ``connect_changed``)."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _changed(self):
        if self._save_source is None:
            self._save_source = GLib.timeout_add(SAVE_DELAY, self._save)

        for callback in list(self._callbacks):
            callback()

    def set_enabled(self, enabled):
        enabled = bool(enabled)

        if enabled == self.enabled:
            return

        self.enabled = enabled
        self._changed()

    def set_replaygain_enabled(self, enabled):
        enabled = bool(enabled)

        if enabled == self.replaygain_enabled:
            return

        self.replaygain_enabled = enabled
        self._changed()

    def set_gain(self, index, value):
        value = _clean_gain(value)

        if value == self.gains[index]:
            return

        self.gains[index] = value
        self._changed()

    def set_gains(self, values):
        gains = [_clean_gain(value) for value in values]

        if len(gains) != BAND_COUNT or gains == self.gains:
            return

        self.gains = gains
        self._changed()

    def effective_gains(self):
        """What the player should apply: the gains, or all zeros (the sound
        untouched) while the equalizer is off."""
        if self.enabled:
            return list(self.gains)

        return [0.0] * BAND_COUNT

    def preset_name(self):
        """The preset the gains are exactly those of, or ``CUSTOM``."""
        for name, gains in PRESETS.items():
            if [float(gain) for gain in gains] == self.gains:
                return name

        return CUSTOM


_equalizer = None


def get_equalizer():
    """The one ``Equalizer`` shared by the whole app."""
    global _equalizer

    if _equalizer is None:
        _equalizer = Equalizer()

    return _equalizer
