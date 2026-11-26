"""The Currently playing page: the track being played, shown large.

Its cover, title, artist and album (the last two open their pages), how long
it lasts, how many times it was played and where its file is, with the
controls to go to the previous or next track, play or pause it, mark it as a
favorite, add it to a playlist or edit it. With nothing playing the page says
so.
"""

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.artwork import get_cached_full_artwork
from melody.services.collection_actions import (
    open_track_album,
    open_track_artist,
    show_in_file_manager,
)
from melody.services.singles import has_album
from melody.services.track_range import track_length
from melody.widgets.link_label import make_link
from melody.widgets.track_table import format_duration, toggle_favorite


class NowPlayingView(Gtk.Box):
    ARTWORK_SIZE = 300

    def __init__(self, playback_manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._title = "Currently playing"
        self.playback_manager = playback_manager
        self._edit_track = None
        self._choose_playlist = None
        # What the page was last built from, and the file whose cover it
        # shows (it is only looked up again for another file).
        self._signature = None
        self._artwork_path = None
        self._artwork_request_id = 0

        self._build_content()

        # The track changes from everywhere (the queue, the player bar, the
        # end of a track...): the page looks for changes while it is shown.
        GLib.timeout_add(250, self._poll)

    @property
    def title(self):
        return self._title

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_content(self):
        self.message = Adw.StatusPage(
            title="Nothing is playing",
            description="Play a track to see it here.",
            icon_name="media-playback-start-symbolic",
        )
        self.message.set_vexpand(True)

        self.artwork = Gtk.Picture()
        self.artwork.set_content_fit(Gtk.ContentFit.COVER)
        self.artwork.set_can_shrink(True)
        self.artwork.set_size_request(self.ARTWORK_SIZE, self.ARTWORK_SIZE)

        placeholder = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        placeholder.set_pixel_size(96)

        self.artwork_stack = Gtk.Stack()
        self.artwork_stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.artwork_stack.set_size_request(
            self.ARTWORK_SIZE,
            self.ARTWORK_SIZE,
        )
        self.artwork_stack.set_halign(Gtk.Align.CENTER)
        self.artwork_stack.add_named(self.artwork, "artwork")
        self.artwork_stack.add_named(placeholder, "placeholder")
        self.artwork_stack.set_visible_child_name("placeholder")

        self.title_label = Gtk.Label(wrap=True)
        self.title_label.add_css_class("title-1")
        self.title_label.set_justify(Gtk.Justification.CENTER)
        self.title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title_label.set_max_width_chars(40)

        self.artist_label = Gtk.Label()
        self.artist_label.add_css_class("title-3")
        self.artist_label.set_halign(Gtk.Align.CENTER)
        make_link(self.artist_label, self._on_artist_clicked)

        self.album_label = Gtk.Label()
        self.album_label.add_css_class("dim-label")
        self.album_label.set_halign(Gtk.Align.CENTER)
        make_link(self.album_label, self._on_album_clicked)

        self.info_label = Gtk.Label()
        self.info_label.add_css_class("dim-label")

        self.path_label = Gtk.Label(wrap=True)
        self.path_label.add_css_class("caption")
        self.path_label.set_halign(Gtk.Align.CENTER)
        self.path_label.set_justify(Gtk.Justification.CENTER)
        self.path_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.path_label.set_max_width_chars(60)
        make_link(self.path_label, self._on_path_clicked)

        controls = self._build_controls()
        actions = self._build_actions()

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_halign(Gtk.Align.CENTER)
        column.set_valign(Gtk.Align.CENTER)
        column.set_margin_top(24)
        column.set_margin_bottom(24)
        column.set_margin_start(24)
        column.set_margin_end(24)

        column.append(self.artwork_stack)
        column.append(self._spaced(self.title_label, 12))
        column.append(self.artist_label)
        column.append(self.album_label)
        column.append(self.info_label)
        column.append(self._spaced(controls, 12))
        column.append(actions)
        column.append(self._spaced(self.path_label, 12))

        self.details_scroll = Gtk.ScrolledWindow()
        self.details_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.details_scroll.set_vexpand(True)
        self.details_scroll.set_child(column)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.message, "message")
        self.stack.add_named(self.details_scroll, "track")
        self.stack.set_visible_child_name("message")

        self.append(self.stack)

    @staticmethod
    def _spaced(widget, top):
        widget.set_margin_top(top)
        return widget

    def _build_controls(self):
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls.set_halign(Gtk.Align.CENTER)

        self.previous_button = Gtk.Button.new_from_icon_name(
            "media-skip-backward-symbolic"
        )
        self.previous_button.set_tooltip_text("Previous")
        self.previous_button.add_css_class("circular")
        self.previous_button.connect("clicked", self._on_previous_clicked)

        self.play_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_button.set_tooltip_text("Play")
        self.play_button.add_css_class("circular")
        self.play_button.add_css_class("suggested-action")
        self.play_button.connect("clicked", self._on_play_clicked)

        self.next_button = Gtk.Button.new_from_icon_name(
            "media-skip-forward-symbolic"
        )
        self.next_button.set_tooltip_text("Next")
        self.next_button.add_css_class("circular")
        self.next_button.connect("clicked", self._on_next_clicked)

        controls.append(self.previous_button)
        controls.append(self.play_button)
        controls.append(self.next_button)
        return controls

    def _build_actions(self):
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)

        self.favorite_button = Gtk.Button(label="Add to favorites")
        self.favorite_button.connect("clicked", self._on_favorite_clicked)

        playlist_button = Gtk.Button(label="Add to playlist…")
        playlist_button.connect("clicked", self._on_playlist_clicked)

        edit_button = Gtk.Button(label="Edit…")
        edit_button.connect("clicked", self._on_edit_clicked)

        actions.append(self.favorite_button)
        actions.append(playlist_button)
        actions.append(edit_button)
        return actions

    # ------------------------------------------------------------------
    # Keeping up with what is playing
    # ------------------------------------------------------------------

    def _poll(self):
        if self.get_mapped():
            track = self.playback_manager.current_track
            signature = (
                id(track),
                track.get("favorite") if track else None,
                track.get("play_count") if track else None,
            )

            if signature != self._signature:
                self._signature = signature
                self._show(track)

            self._update_controls(track)

        return GLib.SOURCE_CONTINUE

    def _update_controls(self, track):
        playing = (
            track is not None and self.playback_manager.state == "playing"
        )

        self.play_button.set_icon_name(
            "media-playback-pause-symbolic"
            if playing
            else "media-playback-start-symbolic"
        )
        self.play_button.set_tooltip_text("Pause" if playing else "Play")

    def _show(self, track):
        if track is None:
            self._artwork_path = None
            self._artwork_request_id += 1
            self.stack.set_visible_child_name("message")
            return

        self.title_label.set_text(str(track.get("title") or "Unknown Title"))
        self.artist_label.set_text(
            str(track.get("artist") or "Unknown Artist")
        )

        album = str(track.get("album") or "") if has_album(track) else ""
        self.album_label.set_text(album)
        self.album_label.set_visible(bool(album))

        count = int(track.get("play_count") or 0)
        plays = (
            "Never played"
            if count == 0
            else f"Played {count} time{'s' if count != 1 else ''}"
        )
        self.info_label.set_text(
            f"{format_duration(track_length(track))} · {plays}"
        )
        self.path_label.set_text(str(track.get("path") or ""))

        self.favorite_button.set_label(
            "Remove from favorites"
            if track.get("favorite")
            else "Add to favorites"
        )

        self.stack.set_visible_child_name("track")

        # The cover is only looked up again when the file is another one (a
        # favorite or a play count changing does not need it).
        path = str(track.get("path") or "")

        if path != self._artwork_path:
            self._artwork_path = path
            self._artwork_request_id += 1
            self.artwork_stack.set_visible_child_name("placeholder")
            self.artwork.set_filename(None)

            if path:
                threading.Thread(
                    target=self._load_artwork,
                    args=(self._artwork_request_id, path),
                    daemon=True,
                ).start()

    def _load_artwork(self, request_id, path):
        try:
            artwork_path = get_cached_full_artwork(Path(path))
        except Exception:
            artwork_path = None

        GLib.idle_add(self._on_artwork_loaded, request_id, artwork_path)

    def _on_artwork_loaded(self, request_id, artwork_path):
        if request_id == self._artwork_request_id and artwork_path:
            self.artwork.set_filename(artwork_path)
            self.artwork_stack.set_visible_child_name("artwork")

        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_previous_clicked(self, button):
        if self.playback_manager.current_track is not None:
            self.playback_manager.previous()

    def _on_next_clicked(self, button):
        if self.playback_manager.current_track is not None:
            self.playback_manager.next()

    def _on_play_clicked(self, button):
        if self.playback_manager.current_track is not None:
            self.playback_manager.toggle_playback()

    def _on_favorite_clicked(self, button):
        track = self.playback_manager.current_track

        if track is not None:
            toggle_favorite(track)

    def _on_playlist_clicked(self, button):
        track = self.playback_manager.current_track

        if track is not None and self._choose_playlist is not None:
            self._choose_playlist(track)

    def _on_edit_clicked(self, button):
        track = self.playback_manager.current_track

        if track is not None and self._edit_track is not None:
            self._edit_track(track)

    def _on_artist_clicked(self):
        track = self.playback_manager.current_track

        if track is not None:
            open_track_artist(track)

    def _on_album_clicked(self):
        track = self.playback_manager.current_track

        if track is not None:
            open_track_album(track)

    def _on_path_clicked(self):
        track = self.playback_manager.current_track

        if track is not None and track.get("path"):
            show_in_file_manager(self.get_root(), track["path"])

    # ------------------------------------------------------------------
    # What the window asks every page
    # ------------------------------------------------------------------

    def play_page(self):
        """The player's Play button has nothing to start from this page."""
        return False
