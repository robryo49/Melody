
import math
import random
import time

import gi

from melody.services.replaygain import MAX_GAIN, read_replaygain
from melody.services.track_range import track_range

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst


class _Deck:
    """A playbin with its own audio chain: the preamp and the equalizer.

    Normally there is one deck. A crossfade briefly has two: the outgoing
    track on the current one, the incoming track on a second one.
    """

    def __init__(self, player, bus, preamp, equalizer):
        self.player = player
        self.bus = bus
        self.preamp = preamp
        self.equalizer = equalizer


class PlaybackManager:
    REPEAT_MODES = ("off", "all", "one")

    # A track counts as played once it has actually been listened to for half
    # of its length, or for this many seconds if that comes first. Skipping
    # through a track does not count, and neither does seeking ahead.
    PLAY_COUNT_MAX_SECONDS = 240.0

    # How long before the end of a track the next one is decided (gapless),
    # and how long before a crossfade starts that it is decided (crossfade
    # decides it a little earlier, so the fade can start on time).
    GAPLESS_PREPARE_SECONDS = 3.0
    CROSSFADE_PREPARE_MARGIN = 1.5

    def __init__(self):
        Gst.init(None)

        # The equalizer's bands, in dB, as GStreamer's ``equalizer-10bands``
        # allows them. Kept here so that every deck gets the same gains.
        self._gains = [0.0] * self.EQUALIZER_BANDS
        # The equalizer sits in the audio path (see ``set_equalizer``). When
        # GStreamer's equalizer plugin is not installed the music plays
        # untouched.
        # The preamp's volume is the product of two things: the equalizer's
        # headroom (see ``set_equalizer``) and the ReplayGain of the track
        # being played (see ``_apply_replaygain``).
        self._equalizer_headroom = 1.0
        self._replaygain_enabled = False
        self._replaygain_factor = 1.0

        self.deck = self._make_deck("melody-player")

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
        # Whether the track starts playing once it is ready, or stays paused
        # (when it was resumed from an earlier session, see ``resume``).
        self._play_when_ready = True

        self.repeat_mode = "off"
        self.shuffle = False
        self._shuffle_remaining = []
        self._shuffle_history = []
        self._shuffle_history_position = -1

        # Gapless playback and crossfade (see ``set_transitions``).
        self.gapless = True
        self.crossfade_seconds = 0.0
        # The next track, decided ahead of time (see ``_prepare_next``), and
        # the one GStreamer has been handed for a gapless change (see
        # ``_on_about_to_finish``). ``_fade`` is a crossfade in progress.
        self._prepared = None
        self._gapless_pending = None
        self._fade = None

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

        self.deck.player.set_property("volume", self.volume)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(250, self._check_transitions)

    @property
    def player(self):
        """The playbin of the track being played (the deck in use)."""
        return self.deck.player

    # The equalizer's bands, in dB, as GStreamer's ``equalizer-10bands``
    # allows them.
    EQUALIZER_BANDS = 10
    EQUALIZER_MIN_GAIN = -24.0
    EQUALIZER_MAX_GAIN = 12.0

    def _make_deck(self, name):
        """A new playbin with its audio chain, listening to its bus."""
        player = Gst.ElementFactory.make("playbin", name)
        if player is None:
            raise RuntimeError("Could not create the GStreamer playbin element")

        audio_filter, preamp, equalizer = self._build_audio_filter()

        if audio_filter is not None:
            player.set_property("audio-filter", audio_filter)

        bus = player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        player.connect("about-to-finish", self._on_about_to_finish)

        deck = _Deck(player, bus, preamp, equalizer)
        self._apply_gains(deck)
        return deck

    def _build_audio_filter(self):
        """The elements the sound goes through: a preamp, then the equalizer.

        The preamp carries the equalizer's headroom and the ReplayGain (see
        ``_update_preamp``). If the equalizer plugin is missing, the preamp
        is kept on its own, so ReplayGain still works. Returns
        ``(filter, preamp, equalizer)``, the last three None if even the
        preamp cannot be built.
        """
        for description in (
            "audioconvert ! volume name=melody-preamp ! "
            "equalizer-10bands name=melody-equalizer ! audioconvert",
            "audioconvert ! volume name=melody-preamp ! audioconvert",
        ):
            try:
                audio_filter = Gst.parse_bin_from_description(
                    description,
                    True,
                )
            except GLib.Error:
                continue

            preamp = audio_filter.get_by_name("melody-preamp")

            if preamp is None:
                continue

            equalizer = audio_filter.get_by_name("melody-equalizer")
            return audio_filter, preamp, equalizer

        return None, None, None

    @property
    def has_equalizer(self):
        """Whether the equalizer can be used (its GStreamer plugin is there)."""
        return self.deck.equalizer is not None

    def _apply_gains(self, deck):
        if deck.equalizer is None:
            return

        for index, gain in enumerate(self._gains):
            deck.equalizer.set_property(f"band{index}", gain)

    def set_equalizer(self, gains):
        """Set the gain of each of the ten bands, in dB (0 leaves it alone).
        Takes effect at once, even in the middle of a track."""
        gains = [
            max(self.EQUALIZER_MIN_GAIN, min(self.EQUALIZER_MAX_GAIN, float(gain)))
            for gain in list(gains)[:self.EQUALIZER_BANDS]
        ]
        gains += [0.0] * (self.EQUALIZER_BANDS - len(gains))
        self._gains = gains

        for deck in self._decks():
            self._apply_gains(deck)

        # Headroom: the loudest boost is taken off the whole sound, so the
        # boosted bands cannot go past the top.
        boost = max(0.0, max(gains))
        self._equalizer_headroom = 10 ** (-boost / 20)
        self._update_preamp()

    def _decks(self):
        """Every deck in use: the one playing, and the incoming one during a
        crossfade."""
        if self._fade is None:
            return [self.deck]

        return [self.deck, self._fade["deck"]]

    def _update_preamp(self):
        """Set the preamp's volume: the equalizer's headroom times the
        ReplayGain of the track being played."""
        preamp = self.deck.preamp

        if preamp is None:
            return

        volume = self._equalizer_headroom * self._replaygain_factor
        preamp.set_property("volume", max(0.0, min(10.0, volume)))

    def set_replaygain_enabled(self, enabled):
        """Play tracks with their ReplayGain tag (or without). Takes effect at
        once, even in the middle of a track."""
        enabled = bool(enabled)

        if enabled == self._replaygain_enabled:
            return

        self._replaygain_enabled = enabled
        self._apply_replaygain()

    def refresh_replaygain(self):
        """The ReplayGain tag of the track being played may have been edited:
        read it again."""
        self._apply_replaygain()

    def _replaygain_factor_for(self, track):
        """The volume correction for ``track``, from its ReplayGain tag.

        A track with no ReplayGain tag, or any trouble reading it, is played
        as it is. A boost never goes past what the track's own peak tag
        allows, so it cannot make it distort.
        """
        factor = 1.0
        path = (track or {}).get("path")

        if self._replaygain_enabled and path:
            gain, peak = read_replaygain(path)

            if gain is not None:
                gain = max(-MAX_GAIN, min(MAX_GAIN, gain))
                factor = 10 ** (gain / 20)

                if peak and peak > 0 and factor * peak > 1.0:
                    factor = 1.0 / peak

        return factor

    def _apply_replaygain(self):
        self._replaygain_factor = self._replaygain_factor_for(self.current_track)
        self._update_preamp()

    # ------------------------------------------------------------------
    # Gapless playback and crossfade
    # ------------------------------------------------------------------

    def set_transitions(self, gapless, crossfade_seconds):
        """How one track follows the next. With a crossfade (more than 0
        seconds) the end of a track fades into the next one, and gapless
        playback is not used; without one, a track is handed over to the next
        without a pause when ``gapless`` is on."""
        self.gapless = bool(gapless)
        self.crossfade_seconds = max(0.0, float(crossfade_seconds or 0))
        self._cancel_transitions()

    @staticmethod
    def _plain(track):
        """A track that plays from its very start to its very end: the only
        kind that can be handed over without a seek."""
        return track_range(track) == (0.0, None)

    def _prepare_next(self):
        """Decide which track plays after the current one, and keep it ready.

        The choice of a shuffled queue changes the shuffle order, so the state
        before it is kept: if the choice is dropped (the person skips, or the
        queue changes), that state comes back and nothing is lost.
        """
        if (
                self._prepared is not None
                or self.current_index < 0
                or not self.queue
                or self.repeat_mode == "one"
        ):
            return

        snapshot = self._shuffle_snapshot()

        if self.shuffle:
            index = self._next_shuffle_index()
        elif self.current_index + 1 < len(self.queue):
            index = self.current_index + 1
        elif self.repeat_mode == "all":
            index = 0
        else:
            index = None

        track = self.queue[index] if index is not None else None
        path = (track or {}).get("path")

        # A track that cannot be handed over (a custom start or end, no file)
        # is left to the normal change at the end, so it is not prepared.
        if track is None or not path or not self._plain(track):
            index = None

        self._prepared = {
            "index": index,
            "track": track,
            "uri": Gst.filename_to_uri(str(path)) if index is not None else None,
            "snapshot": snapshot,
            "version": self.queue_version,
        }

    def _drop_prepared(self):
        """Forget the track prepared for next. The shuffle state is put back
        as it was before it was chosen, unless the queue changed since (then
        its shuffle order was made again already)."""
        prepared = self._prepared
        self._prepared = None
        self._gapless_pending = None

        if prepared is not None and prepared["version"] == self.queue_version:
            self._restore_shuffle(prepared["snapshot"])

    def _shuffle_snapshot(self):
        return (
            list(self._shuffle_history),
            self._shuffle_history_position,
            list(self._shuffle_remaining),
        )

    def _restore_shuffle(self, snapshot):
        history, position, remaining = snapshot
        self._shuffle_history = list(history)
        self._shuffle_history_position = position
        self._shuffle_remaining = list(remaining)

    def _check_transitions(self):
        """Every quarter second: take over a track GStreamer has moved to by
        itself (gapless), and prepare and start a crossfade into the next one.
        """
        if self._gapless_pending is not None:
            # The new track has started from its beginning.
            if self.get_position() < 1.0:
                self._commit_prepared()

            return GLib.SOURCE_CONTINUE

        if (
                self._fade is not None
                or self.current_track is None
                or self.state != "playing"
                or self._pending_seek is not None
        ):
            return GLib.SOURCE_CONTINUE

        _start, end = self._track_range(self.current_track)

        # A track with a custom end stops at that point, which is not the
        # end of its file: it is left to the normal change.
        if end is not None:
            return GLib.SOURCE_CONTINUE

        duration = self.get_duration()

        if duration <= 0:
            return GLib.SOURCE_CONTINUE

        remaining = duration - self.get_position()

        if self.crossfade_seconds > 0:
            if (
                    self._prepared is None
                    and remaining <= self.crossfade_seconds
                    + self.CROSSFADE_PREPARE_MARGIN
            ):
                self._prepare_next()

            prepared = self._prepared

            if (
                    prepared is not None
                    and prepared["index"] is not None
                    and remaining <= self.crossfade_seconds
            ):
                self._start_fade()
        elif (
                self.gapless
                and self._prepared is None
                and remaining <= self.GAPLESS_PREPARE_SECONDS
        ):
            self._prepare_next()

        return GLib.SOURCE_CONTINUE

    def _on_about_to_finish(self, player):
        """GStreamer is about to reach the end of the track: if the next one is
        ready, hand it over now, so it plays with no gap.

        This runs in GStreamer's own thread, so it only passes on what
        ``_prepare_next`` already decided. The rest is done in the main thread
        (see ``_check_transitions``).
        """
        prepared = self._prepared

        if (
                player is not self.deck.player
                or prepared is None
                or prepared["index"] is None
                or self._gapless_pending is not None
                or self._fade is not None
                or self.crossfade_seconds > 0
                or not self.gapless
        ):
            return

        player.set_property("uri", prepared["uri"])
        self._gapless_pending = prepared

    def _commit_prepared(self):
        """The track that was handed over is the one playing now: make it the
        current track (gapless playback)."""
        prepared = self._gapless_pending
        self._prepared = None
        self._gapless_pending = None

        if prepared is None or prepared["index"] is None:
            return

        index = prepared["index"]

        if prepared["version"] != self.queue_version:
            # The queue changed while the track was handed over: find it again.
            index = next(
                (
                    position for position, track in enumerate(self.queue)
                    if track is prepared["track"]
                ),
                None,
            )

            if index is None:
                return

        self._commit_track(index)

    def _commit_track(self, index):
        """Make the queue's track at ``index`` the one being played, without
        loading it again (used once a transition has happened)."""
        track = self.queue[index]
        self.current_index = index
        self.current_track = track
        self._apply_replaygain()
        self._listened = 0.0
        self._play_counted = False

        if self.on_track_changed:
            self.on_track_changed(track)

        self._set_state("playing")

    def _start_fade(self):
        """Start the crossfade: the next track starts on a second deck, quiet,
        and gets louder while the current one gets quieter (see
        ``_fade_step``)."""
        prepared = self._prepared

        if prepared["version"] != self.queue_version:
            self._drop_prepared()
            return

        deck = self._make_deck("melody-fade")
        deck.player.set_property("uri", prepared["uri"])
        deck.player.set_property("volume", 0.0)

        if deck.preamp is not None:
            volume = (
                self._equalizer_headroom
                * self._replaygain_factor_for(prepared["track"])
            )
            deck.preamp.set_property("volume", max(0.0, min(10.0, volume)))

        self._fade = {
            "deck": deck,
            "outgoing": self.deck,
            "started": time.monotonic(),
            "duration": self.crossfade_seconds,
            "prepared": prepared,
        }
        deck.player.set_state(Gst.State.PLAYING)
        GLib.timeout_add(100, self._fade_step)

    def _fade_step(self):
        fade = self._fade

        if fade is None:
            return GLib.SOURCE_REMOVE

        # The queue changed in the middle of the fade: the incoming track is
        # no longer the next one, so the fade is given up.
        if fade["prepared"]["version"] != self.queue_version:
            self._cancel_fade()
            return GLib.SOURCE_REMOVE

        progress = min(
            1.0,
            (time.monotonic() - fade["started"]) / fade["duration"],
        )
        # Equal-power curves: the loudness stays even through the middle.
        angle = progress * math.pi / 2
        fade["outgoing"].player.set_property(
            "volume", self.volume * math.cos(angle)
        )
        fade["deck"].player.set_property(
            "volume", self.volume * math.sin(angle)
        )

        if progress >= 1.0:
            self._finish_fade()
            return GLib.SOURCE_REMOVE

        return GLib.SOURCE_CONTINUE

    def _finish_fade(self):
        """The crossfade is over: the incoming deck becomes the deck in use."""
        fade = self._fade
        self._fade = None

        self._drop_deck(fade["outgoing"])
        self.deck = fade["deck"]
        self._prepared = None
        self.player.set_property("volume", self.volume)
        self._commit_track(fade["prepared"]["index"])

    def _cancel_fade(self):
        fade = self._fade

        if fade is None:
            return

        self._fade = None
        self._drop_deck(fade["deck"])
        fade["outgoing"].player.set_property("volume", self.volume)
        self._drop_prepared()

    def _cancel_transitions(self):
        """Give up whatever was decided or started ahead of time, before the
        queue is moved on by hand."""
        if self._gapless_pending is not None and self.get_position() < 1.0:
            # The player already moved on to the prepared track.
            self._commit_prepared()

        self._cancel_fade()
        self._drop_prepared()

    @staticmethod
    def _drop_deck(deck):
        deck.player.set_state(Gst.State.NULL)
        deck.bus.remove_signal_watch()

    # ------------------------------------------------------------------
    # The queue
    # ------------------------------------------------------------------

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
        self._cancel_transitions()

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

    def play_queued_many_next(self, indexes):
        """Move the tracks at ``indexes`` to right after the one being played,
        keeping their order in the queue. The one being played does not move."""
        if not 0 <= self.current_index < len(self.queue):
            return

        current = self.current_index
        moved = {
            index for index in indexes
            if 0 <= index < len(self.queue) and index != current
        }

        if not moved:
            return

        order = range(len(self.queue))
        before = [index for index in order if index < current and index not in moved]
        after = [index for index in order if index > current and index not in moved]

        self.queue = [
            self.queue[index]
            for index in before + [current] + sorted(moved) + after
        ]
        self.current_index = len(before)
        self.queue_version += 1
        self._reset_shuffle_after_edit()

    def remove_many_from_queue(self, indexes):
        """Take the tracks at ``indexes`` out of the queue. The one being played
        stays. Returns how many were taken out."""
        if not 0 <= self.current_index < len(self.queue):
            return 0

        gone = {
            index for index in indexes
            if 0 <= index < len(self.queue) and index != self.current_index
        }

        if not gone:
            return 0

        self.current_index -= sum(1 for index in gone if index < self.current_index)
        self.queue = [
            track for index, track in enumerate(self.queue)
            if index not in gone
        ]
        self.queue_version += 1
        self._reset_shuffle_after_edit()
        return len(gone)

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
        self._cancel_transitions()
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

    def resume(self, tracks, index, position=0.0, source=None):
        """Put back the queue of an earlier session: ``tracks``, with the one at
        ``index`` loaded at ``position`` (seconds into its file) and paused.
        Play goes on from there."""
        self._cancel_transitions()
        self.queue = list(tracks)
        self.queue_version += 1
        self.source = source
        self.current_index = index
        self._reset_shuffle(index)
        self._load_current_track(resume_at=position)

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

        # What comes next depends on the mode: the prepared track is redone.
        self._cancel_transitions()
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

        self._cancel_transitions()
        self.shuffle = enabled

        if self.current_track is not None:
            self._reset_shuffle(self.current_index)
        else:
            self._reset_shuffle()

    def toggle_shuffle(self):
        self.set_shuffle(not self.shuffle)

    def _load_current_track(self, resume_at=None):
        self._cancel_transitions()

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
        # The track's ReplayGain, before it starts to play.
        self._apply_replaygain()
        # A new play (a repeated track starts over too).
        self._listened = 0.0
        self._play_counted = False

        if self.on_track_changed:
            self.on_track_changed(track)

        self._set_state("paused" if resume_at is not None else "playing")

        start, end = self._track_range(track)

        if resume_at is not None:
            # Back from an earlier session: loaded at its position and kept
            # paused, so it only plays when the person presses play.
            self._pending_seek = max(0.0, float(resume_at))
            self._play_when_ready = False
            self.player.set_state(Gst.State.PAUSED)
        elif start > 0 or end is not None:
            # A track with a custom start and/or end: it can only be seeked
            # once the pipeline is ready, so go to PAUSED first. The seek (and
            # then playing) happens in the bus handler, on ASYNC_DONE.
            self._pending_seek = start
            self._play_when_ready = True
            self.player.set_state(Gst.State.PAUSED)
        else:
            self._pending_seek = None
            self._play_when_ready = True
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
        self._cancel_transitions()

        if self.current_track is None:
            if self.queue:
                self.current_index = 0
                self._reset_shuffle(0)
                self._load_current_track()
            return

        # Playing again: a track still waiting for its start position plays
        # as soon as it is ready.
        self._play_when_ready = True
        self.player.set_state(Gst.State.PLAYING)
        self._set_state("playing")

    def pause(self):
        self._cancel_transitions()

        if self.current_track is None:
            return

        self._play_when_ready = False
        self.player.set_state(Gst.State.PAUSED)
        self._set_state("paused")

    def toggle_playback(self):
        if self.state == "playing":
            self.pause()
        else:
            self.play()

    def stop(self):
        self._cancel_transitions()
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
        self._cancel_transitions()

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
        self._cancel_transitions()

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

        self._cancel_transitions()

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

    def get_resume_position(self):
        """Where the track being played is, for the next start: the position it
        is waiting to start from while the pipeline is not ready yet."""
        if self._pending_seek is not None:
            return self._pending_seek

        return self.get_position()

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
            if self._fade is not None:
                # The outgoing track ended before its fade did (it started
                # late): the incoming track takes over now.
                self._finish_fade()
            elif self.repeat_mode == "one" and self.current_track is not None:
                self._drop_prepared()
                self._load_current_track()
            else:
                self._drop_prepared()
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

                if self._play_when_ready:
                    self.player.set_state(Gst.State.PLAYING)
                    self._set_state("playing")
                else:
                    self._set_state("paused")

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
        self._cancel_transitions()
        self.deck.player.set_state(Gst.State.NULL)
        self.deck.bus.remove_signal_watch()
        self._set_state("stopped")
