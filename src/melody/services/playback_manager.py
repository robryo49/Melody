
import random
import time

import gi

from melody.services.track_range import track_range

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst


class PlaybackManager:
    REPEAT_MODES = ("off", "all", "one")

    # A track counts as played once it has actually been listened to for half
    # of its length, or for this many seconds if that comes first. Skipping
    # through a track does not count, and neither does seeking ahead.
    PLAY_COUNT_MAX_SECONDS = 240.0

    def __init__(self):
        Gst.init(None)

        self.player = Gst.ElementFactory.make("playbin", "melody-player")
        if self.player is None:
            raise RuntimeError("Could not create the GStreamer playbin element")

        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus_message)

        self.queue = []
        # Goes up each time the queue's tracks change, so a view of the queue
        # knows when to be built again.
        self.queue_version = 0
        self.current_index = -1
        self.current_track = None
        # What the queue was started from (an album, an artist, a playlist, the
        # Library...), or None when it was none of those. See
        # ``collection_actions.playback_source``.
        self.source = None
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
        # Called with the new position (seconds into the track) after a
        # seek, so the rest of the desktop can be told (see mpris.py).
        self.on_seeked = None

        # Called with the track when it has been listened to long enough to
        # count as a play (see ``_play_threshold``).
        self.on_play_counted = None
        self._listened = 0.0
        self._play_counted = False
        self._last_tick = time.monotonic()

        self.player.set_property("volume", self.volume)
        GLib.timeout_add(1000, self._tick)

    def set_queue(self, tracks):
        self.queue_version += 1
        self.queue = list(tracks)
        self.current_index = -1
        self.source = None
        self._reset_shuffle()

    def refresh_tracks(self, tracks):
        """The library was reloaded (something was edited, imported or
        scanned): bring the queue up to date without disturbing playback.

        Tracks are matched by id. The queue keeps its tracks and its order,
        but each one becomes the fresh copy (its path, tags and artwork may
        have changed), and the track being played keeps its place in it, so
        repeat, next and previous carry on as before. Tracks that no longer
        exist are dropped. With nothing playing, the queue simply becomes the
        whole library, as with ``set_queue``.
        """
        self.queue_version += 1
        current = self.current_track

        if current is None:
            self.set_queue(tracks)
            return

        fresh = {track.get("id"): track for track in tracks}
        old_ids = [track.get("id") for track in self.queue]
        old_current_index = self.current_index

        # Keep what is still in the library, in the same order. The same
        # track can be queued more than once ("Add to queue"), so remember
        # where each kept entry ended up: the playing one is then still the
        # same entry, not just the first with its id.
        new_queue = []
        index_map = {}

        for old_index, track in enumerate(self.queue):
            if track.get("id") in fresh:
                index_map[old_index] = len(new_queue)
                new_queue.append(fresh[track.get("id")])

        self.queue = new_queue
        new_ids = [track.get("id") for track in self.queue]

        current_id = current.get("id")

        if current_id in fresh:
            # Playing from the fresh copy, so a repeat reloads the file at its
            # new path if an edit moved it.
            self.current_track = fresh[current_id]

            new_index = index_map.get(old_current_index)

            if new_index is None or new_ids[new_index] != current_id:
                if current_id not in new_ids:
                    self.queue.append(self.current_track)
                    new_ids.append(current_id)

                new_index = new_ids.index(current_id)

            self.current_index = new_index
        else:
            # Gone from the library: nothing to keep playing.
            self.stop()
            self.queue = list(tracks)
            self._reset_shuffle()
            return

        if new_ids != old_ids:
            # The queue's shape changed, so the shuffle order no longer fits.
            self._reset_shuffle(self.current_index)

    def play_after_current(self, track):
        """Queue ``track`` to play right after the current one (shuffle
        included). With nothing playing, just play it."""
        if not 0 <= self.current_index < len(self.queue):
            self.play_track(track)
            return

        self._insert_in_queue(self.current_index + 1, track, next_up=True)

    def add_to_queue(self, track):
        """Queue ``track`` at the end (somewhere in what is left when shuffle
        is on). With nothing playing, just play it."""
        if not 0 <= self.current_index < len(self.queue):
            self.play_track(track)
            return

        self._insert_in_queue(len(self.queue), track, next_up=False)

    def play_tracks_after_current(self, tracks):
        """``play_after_current`` for several tracks: they play one after the
        other, in the order given, right after the current one (shuffle
        included). With nothing playing, they are played from the first."""
        tracks = list(tracks)

        if not tracks:
            return

        if not 0 <= self.current_index < len(self.queue):
            self.play_queue(tracks, 0)
            return

        for offset, track in enumerate(tracks):
            self._insert_in_queue(
                self.current_index + 1 + offset,
                track,
                next_up=True,
                history_offset=offset,
            )

    def add_tracks_to_queue(self, tracks):
        """``add_to_queue`` for several tracks, in the order given. With
        nothing playing, they are played from the first."""
        tracks = list(tracks)

        if not tracks:
            return

        if not 0 <= self.current_index < len(self.queue):
            self.play_queue(tracks, 0)
            return

        for track in tracks:
            self._insert_in_queue(len(self.queue), track, next_up=False)

    def play_index(self, index):
        """Play the track at ``index`` of the queue, keeping the queue."""
        if not 0 <= index < len(self.queue):
            return

        self.current_index = index
        self._reset_shuffle(index)
        self._load_current_track()

    def _reset_shuffle_after_edit(self):
        # The shuffle order is made of positions in the queue, which just
        # moved: it is made again around the track being played.
        playing = 0 <= self.current_index < len(self.queue)
        self._reset_shuffle(self.current_index if playing else None)

    def remove_from_queue(self, index):
        """Take the track at ``index`` out of the queue. The track being
        played stays (False is returned for it)."""
        if not 0 <= index < len(self.queue) or index == self.current_index:
            return False

        del self.queue[index]

        if index < self.current_index:
            self.current_index -= 1

        self.queue_version += 1
        self._reset_shuffle_after_edit()
        return True

    def play_queued_next(self, index):
        """Move the track at ``index`` to right after the one being played."""
        if (
                not 0 <= self.current_index < len(self.queue)
                or not 0 <= index < len(self.queue)
                or index == self.current_index
        ):
            return

        track = self.queue.pop(index)

        if index < self.current_index:
            self.current_index -= 1

        self.queue.insert(self.current_index + 1, track)
        self.queue_version += 1
        self._reset_shuffle_after_edit()

    def clear_upcoming(self):
        """Empty the queue of everything but the track being played."""
        if not 0 <= self.current_index < len(self.queue):
            return

        del self.queue[self.current_index + 1:]
        self.queue_version += 1
        self._reset_shuffle_after_edit()

    def _insert_in_queue(self, position, track, next_up, history_offset=0):
        # ``history_offset`` keeps several tracks queued "next" in the order
        # they were given: each goes one place further in the shuffle order.
        self.queue.insert(position, track)
        self.queue_version += 1

        if not self.shuffle:
            return

        # The shuffle order is made of positions in the queue: those from the
        # new track on moved down by one.
        self._shuffle_remaining = [
            index + 1 if index >= position else index
            for index in self._shuffle_remaining
        ]
        self._shuffle_history = [
            index + 1 if index >= position else index
            for index in self._shuffle_history
        ]

        if next_up:
            self._shuffle_history.insert(
                self._shuffle_history_position + 1 + history_offset,
                position,
            )
        else:
            self._shuffle_remaining.insert(
                random.randint(0, len(self._shuffle_remaining)),
                position,
            )

    def play_queue(self, tracks, index=0, source=None):
        """Play ``tracks`` from ``index``. ``source`` says what they are (the
        album, artist, playlist... they were started from)."""
        self.queue = list(tracks)
        self.queue_version += 1

        if not self.queue:
            self.stop()
            return

        if not 0 <= index < len(self.queue):
            raise IndexError("Track index is outside the playback queue")

        self.source = source
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
        # A new play (a repeated track starts over too).
        self._listened = 0.0
        self._play_counted = False

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
        self.source = None
        self._set_state("stopped")

        # Nothing is playing any more (the queue ended, or it was stopped):
        # tell the player bar to go back to "Nothing playing".
        if self.on_track_changed:
            self.on_track_changed(None)

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

        result = self._seek_range(position)

        if result and self.on_seeked:
            start, _end = self._track_range(self.current_track)
            self.on_seeked(max(0.0, float(position) - start))

        return result

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

    def _play_threshold(self):
        """Seconds of listening after which the current track counts as played."""
        length = self.get_clip_duration()

        if length <= 0:
            return 30.0

        return max(1.0, min(length / 2, self.PLAY_COUNT_MAX_SECONDS))

    def _tick(self):
        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now

        if (
                self.state == "playing"
                and self.current_track is not None
                and self._pending_seek is None
                and not self._play_counted
        ):
            # Capped, so a window that was frozen for a while does not count
            # as time listened.
            self._listened += min(elapsed, 2.0)

            if self._listened >= self._play_threshold():
                self._play_counted = True

                if self.on_play_counted:
                    self.on_play_counted(self.current_track)

        return GLib.SOURCE_CONTINUE

    def close(self):
        self.player.set_state(Gst.State.NULL)
        self.bus.remove_signal_watch()
        self._set_state("stopped")
