
import random

import gi

from melody.services.track_range import track_range

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class PlaybackManager:
    REPEAT_MODES = ("off", "all", "one")

    def __init__(self):
        Gst.init(None)

        self.player = Gst.ElementFactory.make("playbin", "melody-player")
        if self.player is None:
            raise RuntimeError("Could not create the GStreamer playbin element")

        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus_message)

        self.queue = []
        self.current_index = -1
        self.current_track = None
        self.state = "stopped"
        self.volume = 0.5

        # Where to seek to once the pipeline is ready (see _load_current_track).
        self._pending_seek = None

        self.repeat_mode = "off"
        self.shuffle = False
        self._shuffle_remaining = []
        self._shuffle_history = []
        self._shuffle_history_position = -1

        self.on_track_changed = None
        self.on_state_changed = None
        self.on_error = None

        self.player.set_property("volume", self.volume)

    def set_queue(self, tracks):
        self.queue = list(tracks)
        self.current_index = -1
        self._reset_shuffle()

    def play_queue(self, tracks, index=0):
        self.queue = list(tracks)

        if not self.queue:
            self.stop()
            return

        if not 0 <= index < len(self.queue):
            raise IndexError("Track index is outside the playback queue")

        self.current_index = index
        self._reset_shuffle(index)
        self._load_current_track()

    def play_track(self, track):
        self.play_queue([track], 0)

    def _reset_shuffle(self, current_index=None):
        self._shuffle_history = []
        self._shuffle_history_position = -1

        if current_index is not None and 0 <= current_index < len(self.queue):
            self._shuffle_history = [current_index]
            self._shuffle_history_position = 0

        self._shuffle_remaining = [
            index for index in range(len(self.queue))
            if index != current_index
        ]
        random.shuffle(self._shuffle_remaining)

    def set_repeat_mode(self, mode):
        if mode not in self.REPEAT_MODES:
            raise ValueError(f"Invalid repeat mode: {mode}")

        self.repeat_mode = mode

    def cycle_repeat_mode(self):
        index = self.REPEAT_MODES.index(self.repeat_mode)
        self.set_repeat_mode(
            self.REPEAT_MODES[(index + 1) % len(self.REPEAT_MODES)]
        )

    def set_shuffle(self, enabled):
        enabled = bool(enabled)

        if self.shuffle == enabled:
            return

        self.shuffle = enabled

        if self.current_track is not None:
            self._reset_shuffle(self.current_index)
        else:
            self._reset_shuffle()

    def toggle_shuffle(self):
        self.set_shuffle(not self.shuffle)

    def _load_current_track(self):
        if not 0 <= self.current_index < len(self.queue):
            self.stop()
            return

        track = self.queue[self.current_index]
        path = track.get("path")

        if not path:
            self._report_error("This track has no file path")
            return

        try:
            uri = Gst.filename_to_uri(str(path))
        except Exception as error:
            self._report_error(str(error))
            return

        self.player.set_state(Gst.State.NULL)
        self.player.set_property("uri", uri)

        self.current_track = track

        if self.on_track_changed:
            self.on_track_changed(track)

        self._set_state("playing")

        start, end = self._track_range(track)

        if start > 0 or end is not None:
            # A track with a custom start and/or end: it can only be seeked
            # once the pipeline is ready, so go to PAUSED first. The seek (and
            # then playing) happens in the bus handler, on ASYNC_DONE.
            self._pending_seek = start
            self.player.set_state(Gst.State.PAUSED)
        else:
            self._pending_seek = None
            self.player.set_state(Gst.State.PLAYING)

    @staticmethod
    def _track_range(track):
        return track_range(track)

    def update_track_range(self, track_id, start, end):
        """A track's start / end was edited: apply it to the queue and, if it
        is the track being played, to the playback right away."""
        for track in (*self.queue, self.current_track):
            if track is not None and track.get("id") == track_id:
                track["start_time"] = start
                track["end_time"] = end

        current = self.current_track

        if (
                current is not None
                and current.get("id") == track_id
                and self._pending_seek is None
        ):
            # Seek to where it is now: that clamps it into the new range and
            # sets the new stop position.
            self.seek(self.get_position())

    def play(self):
        if self.current_track is None:
            if self.queue:
                self.current_index = 0
                self._reset_shuffle(0)
                self._load_current_track()
            return

        self.player.set_state(Gst.State.PLAYING)
        self._set_state("playing")

    def pause(self):
        if self.current_track is None:
            return

        self.player.set_state(Gst.State.PAUSED)
        self._set_state("paused")

    def toggle_playback(self):
        if self.state == "playing":
            self.pause()
        else:
            self.play()

    def stop(self):
        self._pending_seek = None
        self.player.set_state(Gst.State.NULL)
        self.current_track = None
        self.current_index = -1
        self._set_state("stopped")

    def _next_shuffle_index(self):
        if self._shuffle_history_position + 1 < len(self._shuffle_history):
            self._shuffle_history_position += 1
            return self._shuffle_history[self._shuffle_history_position]

        if not self._shuffle_remaining:
            if self.repeat_mode != "all":
                return None

            self._shuffle_remaining = [
                index for index in range(len(self.queue))
                if index != self.current_index
            ]
            random.shuffle(self._shuffle_remaining)

        if not self._shuffle_remaining:
            return None

        index = self._shuffle_remaining.pop()
        self._shuffle_history.append(index)
        self._shuffle_history_position += 1

        return index

    def next(self):
        if not self.queue or self.current_index < 0:
            return

        if self.shuffle:
            next_index = self._next_shuffle_index()

            if next_index is not None:
                self.current_index = next_index
                self._load_current_track()
            else:
                self.stop()

            return

        if self.current_index + 1 < len(self.queue):
            self.current_index += 1
            self._load_current_track()
        elif self.repeat_mode == "all":
            self.current_index = 0
            self._load_current_track()
        else:
            self.stop()

    def previous(self):
        start, _end = self._track_range(self.current_track)
        position = self.get_position() - start

        if position > 3:
            self.seek(start)
            return

        if self.shuffle:
            if self._shuffle_history_position > 0:
                self._shuffle_history_position -= 1
                self.current_index = self._shuffle_history[
                    self._shuffle_history_position
                ]
                self._load_current_track()
            elif self.current_track is not None:
                self.seek(0)

            return

        if self.current_index > 0:
            self.current_index -= 1
            self._load_current_track()
        elif self.current_track is not None:
            if self.repeat_mode == "all":
                self.current_index = len(self.queue) - 1
                self._load_current_track()
            else:
                self.seek(0)

    def seek(self, position):
        if self.current_track is None:
            return False

        # Not ready yet: just change where it will start.
        if self._pending_seek is not None:
            self._pending_seek = max(0.0, float(position))
            return True

        return self._seek_range(position)

    def _seek_range(self, position):
        """Seek to ``position``, kept inside the track's start / end.

        The seek also sets the stop position: when playback reaches the track's
        end, GStreamer posts EOS, exactly as if the file had ended, so the
        usual next track / repeat handling takes over.
        """
        start, end = self._track_range(self.current_track)

        position = max(start, float(position))

        if end is not None:
            position = min(position, max(start, end - 0.05))

        return self.player.seek(
            1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            int(position * Gst.SECOND),
            Gst.SeekType.SET,
            int(end * Gst.SECOND) if end is not None else -1,
        )

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, float(volume)))
        self.player.set_property("volume", self.volume)

    def get_position(self):
        success, position = self.player.query_position(Gst.Format.TIME)
        return position / Gst.SECOND if success else 0.0

    def get_duration(self):
        success, duration = self.player.query_duration(Gst.Format.TIME)
        return duration / Gst.SECOND if success else 0.0

    # Position and length *within the track's start / end*: what the player
    # bar shows. 0 is the track's start time, the duration is end - start.

    def get_clip_position(self):
        start, _end = self._track_range(self.current_track)
        return max(0.0, self.get_position() - start)

    def get_clip_duration(self):
        track = self.current_track
        start, end = self._track_range(track)

        if end is not None:
            stop = end
        else:
            stop = self.get_duration() or float(
                (track or {}).get("duration") or 0
            )

        return max(0.0, stop - start)

    def seek_clip(self, position):
        """Seek to ``position`` seconds after the track's start time."""
        start, _end = self._track_range(self.current_track)
        return self.seek(start + float(position))

    def _set_state(self, state):
        if self.state == state:
            return

        self.state = state

        if self.on_state_changed:
            self.on_state_changed(state)

    def _report_error(self, message):
        self._set_state("stopped")

        if self.on_error:
            self.on_error(message)

    def _on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            if self.repeat_mode == "one" and self.current_track is not None:
                self._load_current_track()
            else:
                self.next()

        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self._report_error(str(error))

        elif message.type == Gst.MessageType.ASYNC_DONE:
            # The pipeline is ready: jump to the track's start (and set its
            # end), then play.
            if message.src == self.player and self._pending_seek is not None:
                position = self._pending_seek
                self._pending_seek = None
                self._seek_range(position)
                self.player.set_state(Gst.State.PLAYING)
                self._set_state("playing")

        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src == self.player:
                old_state, new_state, pending_state = message.parse_state_changed()

                if new_state == Gst.State.PLAYING:
                    self._set_state("playing")
                elif new_state == Gst.State.PAUSED and self._pending_seek is None:
                    self._set_state("paused")

    def close(self):
        self.player.set_state(Gst.State.NULL)
        self.bus.remove_signal_watch()
        self._set_state("stopped")
