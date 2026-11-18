
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.artwork import get_cached_full_artwork
from melody.layout import PAGE_MARGIN, SEARCH_WIDTH, style_header_controls
from melody.services.collection_actions import (
    attach_track_context_menu,
    toggle_track_playback,
    track_is_playing,
)


class LibraryView(Gtk.Box):
    # The window edge, the track list, the artwork and the header controls
    # above them are all PAGE_MARGIN apart, so everything lines up.
    ARTWORK_SIZE = SEARCH_WIDTH
    PANEL_PADDING = PAGE_MARGIN
    PANEL_WIDTH = ARTWORK_SIZE + 2 * PANEL_PADDING
    THUMBNAIL_SIZE = 32

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.tracks = []
        self.playback_manager = None
        self._artwork_request_id = 0
        self._edit_track = None
        self._choose_playlist = None

        self._build_header()
        self._build_list()
        self._build_details_panel()

        self.content_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        self.content_box.set_vexpand(True)

        self.scrolled_window.set_hexpand(True)
        self.content_box.append(self.scrolled_window)
        self.content_box.append(self.details_container)

        self.empty_page = Adw.StatusPage(
            title="Your library is empty",
            description="Add music to your library to get started.",
            icon_name="folder-music-symbolic",
        )
        self.empty_page.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.content_box, "tracks")
        self.stack.add_named(self.empty_page, "empty")

        self.append(self.stack)
        self._update_view()

    def set_playback_manager(self, playback_manager):
        self.playback_manager = playback_manager

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist
        self._refresh_list()

    def _build_header(self):
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        title = Gtk.Label(label="All Tracks", xalign=0)
        title.add_css_class("title-2")
        title.set_hexpand(True)

        self.count_label = Gtk.Label(label="0 tracks")
        self.count_label.add_css_class("dim-label")

        self.sort_dropdown = Gtk.DropDown.new_from_strings([
            "Title",
            "Album",
            "Artist",
            "Duration",
        ])
        self.sort_dropdown.set_selected(0)
        self.sort_dropdown.connect(
            "notify::selected",
            self._on_sort_changed,
        )

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search tracks…")
        style_header_controls(self.sort_dropdown, self.search_entry)
        self.search_entry.connect(
            "search-changed",
            self._on_search_changed,
        )

        header.append(title)
        header.append(self.count_label)
        header.append(self.sort_dropdown)
        header.append(self.search_entry)
        self.append(header)

    def _build_list(self):
        self.track_list = Gtk.ListBox()
        self.track_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.track_list.set_activate_on_single_click(False)
        self.track_list.add_css_class("boxed-list")
        self.track_list.connect(
            "row-selected",
            self._on_selection_changed,
        )
        self.track_list.connect(
            "row-activated",
            self._on_track_activated,
        )

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.scrolled_window.set_vexpand(True)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_margin_start(24)
        # No right margin: the details panel's left padding is the gap.
        self.scrolled_window.set_margin_end(0)
        self.scrolled_window.set_margin_bottom(16)
        self.scrolled_window.set_child(self.track_list)

    def _build_details_panel(self):
        self.details_container = Gtk.Fixed()
        self.details_container.set_size_request(self.PANEL_WIDTH, -1)
        self.details_container.set_hexpand(False)
        self.details_container.set_vexpand(True)
        self.details_container.set_halign(Gtk.Align.START)

        self.details_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        self.details_panel.set_size_request(self.ARTWORK_SIZE, -1)
        self.details_panel.set_hexpand(False)

        self.artwork_stack = Gtk.Stack()
        self.artwork_stack.set_size_request(
            self.ARTWORK_SIZE,
            self.ARTWORK_SIZE,
        )
        self.artwork_stack.set_halign(Gtk.Align.FILL)
        self.artwork_stack.set_valign(Gtk.Align.START)

        self.details_artwork = Gtk.Picture()
        self.details_artwork.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.details_artwork.set_can_shrink(True)
        self.details_artwork.set_size_request(
            self.ARTWORK_SIZE,
            self.ARTWORK_SIZE,
        )

        self.artwork_placeholder = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic"
        )
        self.artwork_placeholder.set_pixel_size(96)

        self.artwork_stack.add_named(self.details_artwork, "artwork")
        self.artwork_stack.add_named(
            self.artwork_placeholder,
            "placeholder",
        )
        self.artwork_stack.set_visible_child_name("placeholder")
        self.details_panel.append(self.artwork_stack)

        self.details_title = Gtk.Label(
            label="No track selected",
            xalign=0,
            wrap=True,
        )
        self.details_title.add_css_class("title-4")
        self.details_title.set_size_request(self.ARTWORK_SIZE, -1)
        self.details_title.set_halign(Gtk.Align.FILL)
        self.details_title.set_justify(Gtk.Justification.CENTER)
        self.details_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.details_title.set_max_width_chars(28)
        self.details_panel.append(self.details_title)

        self.details_artist = self._create_detail_label()
        self.details_album = self._create_detail_label()
        self.details_duration = self._create_detail_label()

        self.details_panel.append(self.details_artist)
        self.details_panel.append(self.details_album)
        self.details_panel.append(self.details_duration)

        self.details_path = self._create_detail_label()
        self.details_path.set_selectable(True)
        self.details_path.add_css_class("caption")
        self.details_panel.append(self.details_path)

        self.details_container.put(
            self.details_panel,
            self.PANEL_PADDING,
            18,
        )

    @staticmethod
    def _create_detail_label():
        label = Gtk.Label(xalign=0, wrap=True)
        label.set_size_request(256, -1)
        label.set_halign(Gtk.Align.FILL)
        label.set_hexpand(False)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(24)
        label.add_css_class("dim-label")
        return label

    def _create_track_row(self, track):
        row = Gtk.ListBoxRow()
        row.track = track

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        content.set_margin_start(10)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        artwork = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic"
        )
        artwork.set_pixel_size(self.THUMBNAIL_SIZE)
        artwork.set_size_request(
            self.THUMBNAIL_SIZE,
            self.THUMBNAIL_SIZE,
        )

        artwork_path = track.get("artwork_path")

        if artwork_path and Path(artwork_path).is_file():
            artwork.set_from_file(artwork_path)
            artwork.set_pixel_size(self.THUMBNAIL_SIZE)

        title = Gtk.Label(
            label=str(track.get("title") or "Unknown Title"),
            xalign=0,
        )
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)

        album = Gtk.Label(
            label=str(track.get("album") or "Unknown Album"),
            xalign=0,
        )
        album.set_size_request(130, -1)
        album.set_ellipsize(Pango.EllipsizeMode.END)
        album.add_css_class("dim-label")
        album.add_css_class("caption")

        artist = Gtk.Label(
            label=str(track.get("artist") or "Unknown Artist"),
            xalign=0,
        )
        artist.set_size_request(120, -1)
        artist.set_ellipsize(Pango.EllipsizeMode.END)
        artist.add_css_class("dim-label")
        artist.add_css_class("caption")

        duration = Gtk.Label(
            label=self._format_duration(track.get("duration")),
            xalign=1,
        )
        duration.set_size_request(52, -1)
        duration.add_css_class("dim-label")
        duration.add_css_class("caption")

        content.append(artwork)
        content.append(title)
        content.append(album)
        content.append(artist)
        content.append(duration)

        row.set_child(content)
        if self._edit_track is not None:
            attach_track_context_menu(
                row,
                track,
                on_play_pause=self._toggle_track,
                is_playing=lambda t: track_is_playing(self.playback_manager, t),
                on_edit=self._edit_track,
                on_add_to_playlist=self._choose_playlist,
            )
        return row

    def _toggle_track(self, track):
        toggle_track_playback(
            self.playback_manager,
            track,
            self._get_sorted_tracks(self.tracks),
        )

    def _on_track_activated(self, list_box, row):
        if self.playback_manager is None or not self.tracks:
            return

        selected_track = row.track
        queue = self._get_sorted_tracks(self.tracks)

        for index, track in enumerate(queue):
            if track.get("path") == selected_track.get("path"):
                self.playback_manager.play_queue(queue, index)
                return

    def _on_selection_changed(self, list_box, row):
        self._artwork_request_id += 1
        request_id = self._artwork_request_id

        if row is None:
            self._clear_details()
            return

        track = row.track
        track_path = str(track.get("path") or "")

        self.details_title.set_text(
            str(track.get("title") or "Unknown Title")
        )
        self.details_artist.set_text(
            str(track.get("artist") or "Unknown Artist")
        )
        self.details_album.set_text(
            str(track.get("album") or "Unknown Album")
        )
        self.details_duration.set_text(
            self._format_duration(track.get("duration"))
        )
        self.details_path.set_text(track_path)

        self.artwork_stack.set_visible_child_name("placeholder")
        self.details_artwork.set_filename(None)

        if track_path:
            threading.Thread(
                target=self._load_full_artwork,
                args=(request_id, track_path),
                daemon=True,
            ).start()

    def _load_full_artwork(self, request_id, track_path):
        try:
            artwork_path = get_cached_full_artwork(Path(track_path))
        except Exception:
            artwork_path = None

        GLib.idle_add(
            self._on_full_artwork_loaded,
            request_id,
            track_path,
            artwork_path,
        )

    def _on_full_artwork_loaded(self, request_id, track_path, artwork_path):
        if request_id != self._artwork_request_id:
            return GLib.SOURCE_REMOVE

        row = self.track_list.get_selected_row()

        if row is None:
            return GLib.SOURCE_REMOVE

        current_path = str(row.track.get("path") or "")

        if current_path != track_path:
            return GLib.SOURCE_REMOVE

        if artwork_path:
            self.details_artwork.set_filename(artwork_path)
            self.artwork_stack.set_visible_child_name("artwork")

        return GLib.SOURCE_REMOVE

    def _clear_details(self):
        self.artwork_stack.set_visible_child_name("placeholder")
        self.details_artwork.set_filename(None)
        self.details_title.set_text("No track selected")
        self.details_artist.set_text("")
        self.details_album.set_text("")
        self.details_duration.set_text("")
        self.details_path.set_text("")

    @staticmethod
    def _format_duration(duration):
        duration = max(0, int(duration or 0))
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours:
            return f"{hours}:{minutes:02}:{seconds:02}"

        return f"{minutes}:{seconds:02}"

    def _get_sorted_tracks(self, tracks):
        sort_index = self.sort_dropdown.get_selected()
        fields = ("title", "album", "artist", "duration")
        field = fields[sort_index]

        if field == "duration":
            return sorted(
                tracks,
                key=lambda track: float(track.get("duration") or 0),
            )

        return sorted(
            tracks,
            key=lambda track: str(track.get(field) or "").casefold(),
        )

    def _get_filtered_tracks(self):
        query = self.search_entry.get_text().strip().casefold()

        if not query:
            return list(self.tracks)

        return [
            track for track in self.tracks
            if any(
                query in str(track.get(field) or "").casefold()
                for field in ("title", "album", "artist")
            )
        ]

    def _refresh_list(self):
        child = self.track_list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.track_list.remove(child)
            child = next_child

        visible_tracks = self._get_sorted_tracks(
            self._get_filtered_tracks()
        )

        for track in visible_tracks:
            self.track_list.append(self._create_track_row(track))

        self._update_view()

    def _on_search_changed(self, search_entry):
        self._refresh_list()

    def _on_sort_changed(self, dropdown, pspec):
        self._refresh_list()

    def set_tracks(self, tracks):
        self.tracks = list(tracks)
        self._refresh_list()

    def _update_view(self):
        total = len(self.tracks)
        visible = self.track_list.get_row_at_index(0) is not None
        visible_count = len(self._get_filtered_tracks())

        if total == 0:
            self.empty_page.set_title("Your library is empty")
            self.empty_page.set_description(
                "Add music to your library to get started."
            )
            self.stack.set_visible_child_name("empty")
        elif not visible:
            self.empty_page.set_title("No matching tracks")
            self.empty_page.set_description("Try a different search.")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("tracks")

        if visible_count == total:
            self.count_label.set_text(
                f"{total} track{'s' if total != 1 else ''}"
            )
        else:
            self.count_label.set_text(
                f"{visible_count} of {total} tracks"
            )
