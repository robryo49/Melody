"""MPRIS: lets the rest of the desktop see and control Melody.

MPRIS is the D-Bus interface that media keys, the shell's media widget (the
one with the play / next buttons), ``playerctl`` and remote-control apps use.
Melody publishes itself on the session bus as ``org.mpris.MediaPlayer2.melody``
and offers:

* what is playing (title, artist, album, length, cover) and where it is;
* play, pause, stop, next, previous, seek and set position;
* the loop (repeat) mode, shuffle and volume, which can also be changed.

It has no GTK code: it works on a ``PlaybackManager`` and calls a few
functions given by the window for what only the window knows how to do
(starting playback like the play button, raising the window...).

Nothing tells this service when the player changes, so it looks at the
player a few times a second and announces whatever changed (the standard
``PropertiesChanged`` signal). That also covers changes made from Melody's
own player bar.
"""

import re

from gi.repository import Gio, GLib


BUS_NAME = "org.mpris.MediaPlayer2.melody"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

POLL_INTERVAL_MS = 300

# MPRIS names for the player's repeat modes, and back.
LOOP_STATUS = {"off": "None", "one": "Track", "all": "Playlist"}
REPEAT_MODE = {status: mode for mode, status in LOOP_STATUS.items()}

PLAYBACK_STATUS = {"playing": "Playing", "paused": "Paused"}

INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class _NotSupported(Exception):
    pass


def track_object_path(track):
    """The D-Bus object path that identifies ``track`` (letters, digits and
    underscores only, which is all a path may hold)."""
    if track is None:
        return NO_TRACK

    name = str(track.get("id") or track.get("path") or "track")
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return f"/org/mpris/MediaPlayer2/Track/{name}"


class MprisService:
    """Publishes a ``PlaybackManager`` on the session bus.

    The window gives the few things only it can do:

    * ``play()``: start playing from nothing, like the player bar's play
      button (which plays the selected track, if any);
    * ``raise_window()`` and ``quit_app()``;
    * ``sync_options()``: refresh the repeat and shuffle buttons after they
      were changed from outside;
    * ``set_volume(volume)``: move the volume slider (and so the volume).
    """

    def __init__(
            self,
            playback_manager,
            *,
            play=None,
            raise_window=None,
            quit_app=None,
            sync_options=None,
            set_volume=None,
    ):
        self.manager = playback_manager
        self._play_from_stopped = play
        self._raise_window = raise_window
        self._quit_app = quit_app
        self._sync_options = sync_options
        self._set_volume = set_volume

        self._owner_id = 0
        self._connection = None
        self._registrations = []
        self._poll_id = 0
        self._snapshot = {}

    # ------------------------------------------------------------------
    # Starting and stopping
    # ------------------------------------------------------------------

    def start(self):
        """Publish Melody. Does nothing (but say so) if there is no session
        bus: the player works fine without being controllable."""
        try:
            self._owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION,
                BUS_NAME,
                Gio.BusNameOwnerFlags.NONE,
                self._on_bus_acquired,
                None,
                None,
            )
        except Exception as error:
            print(f"MPRIS is not available: {error}")
            return

        self.manager.on_seeked = self._on_seeked

    def close(self):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

        if self.manager.on_seeked == self._on_seeked:
            self.manager.on_seeked = None

        if self._connection is not None:
            for registration in self._registrations:
                self._connection.unregister_object(registration)

            self._registrations = []

        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0

        self._connection = None

    def _on_bus_acquired(self, connection, _name):
        try:
            node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

            for interface in node.interfaces:
                self._registrations.append(
                    connection.register_object(
                        OBJECT_PATH,
                        interface,
                        self._on_method_call,
                        self._on_get_property,
                        self._on_set_property,
                    )
                )
        except Exception as error:
            print(f"MPRIS could not be published: {error}")
            return

        self._connection = connection
        self._snapshot = self._snapshot_of(self._player_properties())
        self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def _metadata(self, track):
        if track is None:
            return {"mpris:trackid": GLib.Variant("o", NO_TRACK)}

        metadata = {
            "mpris:trackid": GLib.Variant("o", track_object_path(track)),
            "xesam:title": GLib.Variant(
                "s", str(track.get("title") or "Unknown Title")
            ),
            "xesam:artist": GLib.Variant(
                "as", [str(track.get("artist") or "Unknown Artist")]
            ),
        }

        album = str(track.get("album") or "")

        if album:
            metadata["xesam:album"] = GLib.Variant("s", album)

        album_artist = str(track.get("album_artist") or "")

        if album_artist:
            metadata["xesam:albumArtist"] = GLib.Variant("as", [album_artist])

        genre = str(track.get("genre") or "")

        if genre:
            metadata["xesam:genre"] = GLib.Variant("as", [genre])

        length = self.manager.get_clip_duration()

        if length > 0:
            metadata["mpris:length"] = GLib.Variant(
                "x", int(length * 1_000_000)
            )

        for key in ("album_artwork_path", "artwork_path"):
            path = track.get(key)

            if path and Gio.File.new_for_path(str(path)).query_exists(None):
                metadata["mpris:artUrl"] = GLib.Variant(
                    "s", Gio.File.new_for_path(str(path)).get_uri()
                )
                break

        return metadata

    def _player_properties(self):
        """Every property of the Player interface, as variants."""
        manager = self.manager
        track = manager.current_track
        has_track = track is not None

        return {
            "PlaybackStatus": GLib.Variant(
                "s", PLAYBACK_STATUS.get(manager.state, "Stopped")
            ),
            "LoopStatus": GLib.Variant(
                "s", LOOP_STATUS.get(manager.repeat_mode, "None")
            ),
            "Rate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", bool(manager.shuffle)),
            "Metadata": GLib.Variant("a{sv}", self._metadata(track)),
            "Volume": GLib.Variant("d", float(manager.volume)),
            "Position": GLib.Variant("x", self._position()),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", has_track),
            "CanGoPrevious": GLib.Variant("b", has_track),
            "CanPlay": GLib.Variant("b", has_track or bool(manager.queue)),
            "CanPause": GLib.Variant("b", has_track),
            "CanSeek": GLib.Variant("b", has_track),
            "CanControl": GLib.Variant("b", True),
        }

    @staticmethod
    def _root_properties():
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", "Melody"),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
        }

    def _position(self):
        if self.manager.current_track is None:
            return 0

        return int(self.manager.get_clip_position() * 1_000_000)

    @staticmethod
    def _snapshot_of(properties):
        # The position changes all the time and is not announced (clients
        # ask for it, and are told about jumps by ``Seeked``).
        return {
            name: str(value)
            for name, value in properties.items()
            if name != "Position"
        }

    def _on_get_property(
            self, _connection, _sender, _path, interface, name,
    ):
        if interface == ROOT_INTERFACE:
            return self._root_properties().get(name)

        if interface == PLAYER_INTERFACE:
            return self._player_properties().get(name)

        return None

    def _on_set_property(
            self, _connection, _sender, _path, interface, name, value,
    ):
        if interface != PLAYER_INTERFACE:
            return False

        if name == "LoopStatus":
            mode = REPEAT_MODE.get(value.get_string())

            if mode is None:
                return False

            self.manager.set_repeat_mode(mode)
            self._refresh_options()

        elif name == "Shuffle":
            self.manager.set_shuffle(value.get_boolean())
            self._refresh_options()

        elif name == "Volume":
            volume = max(0.0, min(1.0, value.get_double()))

            if self._set_volume is not None:
                self._set_volume(volume)
            else:
                self.manager.set_volume(volume)

        elif name == "Rate":
            # Only the normal speed exists: accept it, change nothing.
            pass

        else:
            return False

        return True

    def _refresh_options(self):
        if self._sync_options is not None:
            self._sync_options()

    # ------------------------------------------------------------------
    # Telling the desktop what changed
    # ------------------------------------------------------------------

    def _poll(self):
        properties = self._player_properties()
        snapshot = self._snapshot_of(properties)

        changed = {
            name: properties[name]
            for name, text in snapshot.items()
            if text != self._snapshot.get(name)
        }
        self._snapshot = snapshot

        if changed:
            self._emit_properties_changed(PLAYER_INTERFACE, changed)

        return GLib.SOURCE_CONTINUE

    def _emit_properties_changed(self, interface, changed):
        if self._connection is None:
            return

        self._connection.emit_signal(
            None,
            OBJECT_PATH,
            PROPERTIES_INTERFACE,
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (interface, changed, [])),
        )

    def _on_seeked(self, clip_position):
        """The player jumped to ``clip_position`` seconds into the track."""
        if self._connection is None:
            return

        self._connection.emit_signal(
            None,
            OBJECT_PATH,
            PLAYER_INTERFACE,
            "Seeked",
            GLib.Variant("(x)", (int(clip_position * 1_000_000),)),
        )

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def _on_method_call(
            self,
            _connection,
            _sender,
            _path,
            _interface,
            method,
            parameters,
            invocation,
    ):
        handler = getattr(self, f"_method_{method}", None)

        if handler is None:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method: {method}",
            )
            return

        try:
            handler(*parameters.unpack())
        except _NotSupported as error:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.NotSupported",
                str(error),
            )
            return
        except Exception as error:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.Failed",
                str(error),
            )
            return

        invocation.return_value(None)

    def _method_Raise(self):
        if self._raise_window is not None:
            self._raise_window()

    def _method_Quit(self):
        if self._quit_app is not None:
            self._quit_app()

    def _method_Next(self):
        self.manager.next()

    def _method_Previous(self):
        self.manager.previous()

    def _method_Pause(self):
        self.manager.pause()

    def _method_Stop(self):
        self.manager.stop()

    def _method_Play(self):
        self._start_playing()

    def _method_PlayPause(self):
        if self.manager.state == "playing":
            self.manager.pause()
        else:
            self._start_playing()

    def _start_playing(self):
        manager = self.manager

        if manager.state == "playing":
            return

        if manager.current_track is None and self._play_from_stopped:
            # Like the play button: the selected track, if there is one.
            self._play_from_stopped()
        else:
            manager.play()

    def _method_Seek(self, offset):
        manager = self.manager

        if manager.current_track is None:
            return

        target = max(0.0, manager.get_clip_position() + offset / 1_000_000)
        duration = manager.get_clip_duration()

        # Past the end of the track is the same as asking for the next one.
        if duration > 0 and target >= duration:
            manager.next()
            return

        manager.seek_clip(target)

    def _method_SetPosition(self, track_id, position):
        manager = self.manager
        track = manager.current_track

        # A request meant for a track that is no longer the current one is
        # ignored, as the specification asks.
        if track is None or track_id != track_object_path(track):
            return

        seconds = position / 1_000_000
        duration = manager.get_clip_duration()

        if seconds < 0 or (duration > 0 and seconds > duration):
            return

        manager.seek_clip(seconds)

    def _method_OpenUri(self, _uri):
        raise _NotSupported("Melody cannot open a link: import it instead.")
