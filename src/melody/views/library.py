
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.artwork import get_cached_full_artwork
from melody.services.track_range import track_length
from melody.layout import PAGE_MARGIN, SEARCH_WIDTH, style_header_controls
from melody.services.collection_actions import (
    attach_track_context_menu,
    is_playback_source,
    open_track_album,
    open_track_artist,
    playback_source,
    scroll_to_row,
    show_in_file_manager,
    toggle_track_playback,
    track_is_playing,
)
from melody.widgets.link_label import make_link
from melody.widgets.track_table import (
    TrackSort,
    TrackTableHeader,
    create_track_row,
    toggle_favorite,
    update_favorite_in_list,
    update_play_count_in_list,
)


class LibraryView(Gtk.Box):
    # The window edge, the track list, the artwork and the header controls
    # above them are all PAGE_MARGIN apart, so everything lines up.
    ARTWORK_SIZE = SEARCH_WIDTH
    PANEL_PADDING = PAGE_MARGIN
    PANEL_WIDTH = ARTWORK_SIZE + 2 * PANEL_PADDING
    THUMBNAIL_SIZE = 32

    def __init__(
            self,
            title="All Tracks",
            noun="track",
            search_placeholder="Search tracks…",
            empty_title="Your library is empty",
            empty_description="Add music to your library to get started.",
            empty_icon="folder-music-symbolic",
            track_filter=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # The Library and the Compilations page are this same view; they only
        # differ by these texts and by which tracks they accept.
        self._title = title
        self._noun = noun
        self._search_placeholder = search_placeholder
        self._empty_title = empty_title
        self._empty_description = empty_description
        self._empty_icon = empty_icon
        self._track_filter = track_filter

        self.tracks = []
        # What the list shows right now, filtered and in its sort order: the
        # queue that every way of playing from this page uses.
        self._displayed_tracks = []
        self.playback_manager = None
        self._artwork_request_id = 0
        self._edit_track = None
        self._choose_playlist = None
        # The column the list is sorted by (clicked in the header above it).
        self.track_sort = TrackSort()

        self._build_header()
        self._build_list()
        self._build_details_panel()

        self.content_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        self.content_box.set_vexpand(True)

        self.scrolled_window.set_hexpand(True)
        self.content_box.append(self.list_column)
        self.content_box.append(self.details_container)

        self.empty_page = Adw.StatusPage(
            title=self._empty_title,
            description=self._empty_description,
            icon_name=self._empty_icon,
        )
        self.empty_page.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.content_box, "tracks")
        self.stack.add_named(self.empty_page, "empty")

        self.append(self.stack)
        self._update_view()

    @property
    def title(self):
        return self._title

    def show_track(self, track):
        """Select ``track`` in the list and scroll to it. A search that hides
        it is cleared first."""
        track_id = track.get("id")
        delay = 80

        shown = any(
            candidate.get("id") == track_id
            for candidate in self._displayed_tracks
        )

        if not shown and self.search_entry.get_text():
            self.search_entry.set_text("")
            self._refresh_list()
            # The search entry reports its change a moment later, which
            # rebuilds the list once more: wait for that before scrolling.
            delay = 400

        GLib.timeout_add(delay, self._select_and_scroll, track_id)

    def _select_and_scroll(self, track_id):
        child = self.track_list.get_first_child()

        while child is not None:
            if child.track.get("id") == track_id:
                self.track_list.select_row(child)
                scroll_to_row(child)
                break

            child = child.get_next_sibling()

        return GLib.SOURCE_REMOVE

    def set_playback_manager(self, playback_manager):
        self.playback_manager = playback_manager
        GLib.timeout_add(250, self._update_action_buttons)

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist
        self._refresh_list()

    def set_import_callback(self, callback):
        """Show an "Import" button in the header that calls ``callback()``.

        Only the Library page gets one; without this call the button stays
        hidden (the Compilations page is this same view).
        """
        self.import_button.connect("clicked", lambda _button: callback())
        self.import_button.set_visible(True)

    def _build_header(self):
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        title = Gtk.Label(label=self._title, xalign=0)
        title.add_css_class("title-2")
        title.set_hexpand(True)

        self.count_label = Gtk.Label(label=f"0 {self._noun}s")
        self.count_label.add_css_class("dim-label")

        self.import_button = Gtk.Button()
        self.import_button.set_child(
            Adw.ButtonContent(
                icon_name="list-add-symbolic",
                label="Import",
            )
        )
        self.import_button.set_tooltip_text("Import music into your library")
        self.import_button.set_visible(False)

        # Plays the list below, in the order it is sorted.
        self.play_all_button = Gtk.Button(label="Play")
        self.play_all_button.set_tooltip_text("Play the list below")
        self.play_all_button.set_sensitive(False)
        self.play_all_button.connect(
            "clicked",
            self._on_play_all_clicked,
        )

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(self._search_placeholder)
        # The list is sorted from the column names above it: no dropdown.
        style_header_controls(None, self.search_entry)
        self.search_entry.connect(
            "search-changed",
            self._on_search_changed,
        )

        header.append(title)
        header.append(self.count_label)
        header.append(self.play_all_button)
        header.append(self.import_button)
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

        # The column names stay above the scrolled rows, so they are always
        # in view; both share the list's margins to line up.
        self.track_header = TrackTableHeader(
            self.track_sort,
            self._refresh_list,
            with_artwork=True,
            margin_start=24,
            margin_end=0,
        )
        self.track_header.set_margin_top(4)

        self.list_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        self.list_column.set_hexpand(True)
        self.list_column.set_vexpand(True)
        self.list_column.append(self.track_header)
        self.list_column.append(self.scrolled_window)

    def _build_details_panel(self):
        # The container stacks the track info (top) and the action buttons
        # (pinned to the bottom of the panel).
        self.details_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        self.details_container.set_size_request(self.PANEL_WIDTH, -1)
        self.details_container.set_hexpand(False)
        self.details_container.set_vexpand(True)
        self.details_container.set_halign(Gtk.Align.START)

        details_fixed = Gtk.Fixed()
        details_fixed.set_vexpand(True)

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
        # The music note fills only part of the artwork's area, like the
        # other pages' placeholders do (about 70% of the old size).
        self.artwork_placeholder.set_pixel_size(67)

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
        self.details_plays = self._create_detail_label()

        # The artist, the album and the path are links: they open the
        # artist's page, the album's page and the folder of the file. Only
        # their text reacts, so they are not stretched across the panel.
        for label, callback in (
            (self.details_artist, self._on_artist_link_clicked),
            (self.details_album, self._on_album_link_clicked),
        ):
            label.set_halign(Gtk.Align.START)
            label.set_size_request(-1, -1)
            make_link(label, callback)

        self.details_panel.append(self.details_artist)
        self.details_panel.append(self.details_album)
        self.details_panel.append(self.details_duration)
        self.details_panel.append(self.details_plays)

        self.details_path = self._create_detail_label()
        self.details_path.set_halign(Gtk.Align.START)
        self.details_path.set_size_request(-1, -1)
        self.details_path.add_css_class("caption")
        make_link(self.details_path, self._on_path_link_clicked)
        self.details_panel.append(self.details_path)

        details_fixed.put(
            self.details_panel,
            self.PANEL_PADDING,
            18,
        )

        self.details_container.append(details_fixed)
        self.details_container.append(self._build_action_buttons())

    def _build_action_buttons(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        box.set_margin_start(self.PANEL_PADDING)
        box.set_margin_end(self.PANEL_PADDING)
        box.set_margin_top(12)
        box.set_margin_bottom(16)

        self.play_button = Gtk.Button(label="Play")
        self.play_button.add_css_class("suggested-action")
        self.play_button.connect("clicked", self._on_play_button_clicked)

        self.edit_button = Gtk.Button(label="Edit…")
        self.edit_button.connect("clicked", self._on_edit_button_clicked)

        self.playlist_button = Gtk.Button(label="Add to playlist…")
        self.playlist_button.connect(
            "clicked",
            self._on_playlist_button_clicked,
        )

        self.favorite_button = Gtk.Button(label="Add to favorites")
        self.favorite_button.connect(
            "clicked",
            self._on_favorite_button_clicked,
        )

        for button in (
            self.play_button,
            self.edit_button,
            self.playlist_button,
            self.favorite_button,
        ):
            button.set_sensitive(False)
            box.append(button)

        return box

    def _selected_track(self):
        row = self.track_list.get_selected_row()
        return row.track if row is not None else None

    def _update_action_buttons(self):
        track = self._selected_track()
        has_track = track is not None

        for button in (
            self.play_button,
            self.edit_button,
            self.playlist_button,
            self.favorite_button,
        ):
            button.set_sensitive(has_track)

        self.favorite_button.set_label(
            "Remove from favorites"
            if has_track and track.get("favorite")
            else "Add to favorites"
        )

        playing = has_track and track_is_playing(self.playback_manager, track)
        self.play_button.set_label("Pause" if playing else "Play")

        self.play_all_button.set_sensitive(bool(self._displayed_tracks))
        self.play_all_button.set_label(
            "Pause"
            if (
                self.playback_manager is not None
                and self.playback_manager.state == "playing"
                and self._is_list_current()
            )
            else "Play"
        )

        return GLib.SOURCE_CONTINUE

    def _is_list_current(self):
        """True when the list is what is being played: it was started from
        this page, not merely a track of it playing."""
        return is_playback_source(
            self.playback_manager,
            "list",
            self._title,
        )

    def _list_source(self):
        return playback_source("list", self._title, self._displayed_tracks)

    def _on_play_all_clicked(self, button):
        manager = self.playback_manager

        if manager is None or not self._displayed_tracks:
            return

        if self._is_list_current():
            manager.toggle_playback()
        else:
            manager.play_queue(self._displayed_tracks, 0, self._list_source())

        self._update_action_buttons()

    def play_page(self):
        """Play the list from its first track, as the Play button does.

        False (and nothing played) when the list is empty.
        """
        if self.playback_manager is None or not self._displayed_tracks:
            return False

        self.playback_manager.play_queue(
            self._displayed_tracks,
            0,
            self._list_source(),
        )
        return True

    def _on_play_button_clicked(self, button):
        track = self._selected_track()

        if track is not None:
            self._toggle_track(track)
            self._update_action_buttons()

    def _on_artist_link_clicked(self):
        track = self._selected_track()

        if track is not None:
            open_track_artist(track)

    def _on_album_link_clicked(self):
        track = self._selected_track()

        if track is not None:
            open_track_album(track)

    def _on_path_link_clicked(self):
        track = self._selected_track()

        if track is not None and track.get("path"):
            show_in_file_manager(self.get_root(), track["path"])

    def _on_edit_button_clicked(self, button):
        track = self._selected_track()

        if track is not None and self._edit_track is not None:
            self._edit_track(track)

    def _on_favorite_button_clicked(self, button):
        track = self._selected_track()

        if track is not None:
            toggle_favorite(track)

    def _on_playlist_button_clicked(self, button):
        track = self._selected_track()

        if track is not None and self._choose_playlist is not None:
            self._choose_playlist(track)

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
        artwork = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic"
        )
        # The thumbnail keeps its size; only the music note inside it is
        # smaller. A real cover (below) is drawn at the full size.
        artwork.set_pixel_size(round(self.THUMBNAIL_SIZE * 0.7))
        artwork.set_size_request(
            self.THUMBNAIL_SIZE,
            self.THUMBNAIL_SIZE,
        )

        artwork_path = track.get("artwork_path")

        if artwork_path and Path(artwork_path).is_file():
            artwork.set_from_file(artwork_path)
            artwork.set_pixel_size(self.THUMBNAIL_SIZE)

        row = create_track_row(
            track,
            artwork=artwork,
            vertical_margin=6,
        )

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
            self._displayed_tracks,
            self._list_source(),
        )

    def _on_track_activated(self, list_box, row):
        if self.playback_manager is None or not self._displayed_tracks:
            return

        # Like the Play button, but starting from the track that was clicked.
        selected_track = row.track
        queue = self._displayed_tracks

        for index, track in enumerate(queue):
            if track.get("path") == selected_track.get("path"):
                self.playback_manager.play_queue(
                    queue,
                    index,
                    self._list_source(),
                )
                return

    def play_selected(self):
        """Play the selected track, as a double click on it would.

        False (and nothing played) when no track is selected.
        """
        row = self.track_list.get_selected_row()

        if row is None or self.playback_manager is None:
            return False

        self._on_track_activated(self.track_list, row)
        return True

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
            str(track.get("album") or "")
        )
        self.details_duration.set_text(
            self._format_duration(track_length(track))
        )
        self.details_path.set_text(track_path)
        self.details_plays.set_text(self._plays_text(track))
        self._update_action_buttons()

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
        self.details_plays.set_text("")
        self.details_path.set_text("")
        self._update_action_buttons()

    @staticmethod
    def _format_duration(duration):
        duration = max(0, int(duration or 0))
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours:
            return f"{hours}:{minutes:02}:{seconds:02}"

        return f"{minutes}:{seconds:02}"

    @staticmethod
    def _plays_text(track):
        count = int(track.get("play_count") or 0)

        if count == 0:
            return "Never played"

        return f"Played {count} time{'s' if count != 1 else ''}"

    def update_play_count(self, track_id, count):
        """A track was played once more: update its row in the list and, if
        it is the selected track, the count shown in the details."""
        update_play_count_in_list(self.track_list, track_id, count)

        track = self._selected_track()

        if track is not None and track.get("id") == track_id:
            self.details_plays.set_text(self._plays_text(track))

    def update_favorite(self, track_id, favorite):
        """A track was marked or unmarked as a favorite: update its heart."""
        update_favorite_in_list(self.track_list, track_id, favorite)

    def _get_sorted_tracks(self, tracks):
        return self.track_sort.sort(tracks)

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
        # The selected track stays selected (and its details stay on screen)
        # when the list is rebuilt: after an edit window is saved, a search,
        # a new sort order...
        selected = self._selected_track()
        selected_id = selected.get("id") if selected is not None else None

        child = self.track_list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.track_list.remove(child)
            child = next_child

        visible_tracks = self._get_sorted_tracks(
            self._get_filtered_tracks()
        )
        self._displayed_tracks = visible_tracks

        selected_row = None

        for track in visible_tracks:
            row = self._create_track_row(track)
            self.track_list.append(row)

            if selected_id is not None and track.get("id") == selected_id:
                selected_row = row

        if selected_row is not None:
            self.track_list.select_row(selected_row)

        self._update_view()

    def _on_search_changed(self, search_entry):
        self._refresh_list()

    def set_tracks(self, tracks):
        self.tracks = [
            track for track in tracks
            if self._track_filter is None or self._track_filter(track)
        ]
        self._refresh_list()

    def _update_view(self):
        total = len(self.tracks)
        visible = self.track_list.get_row_at_index(0) is not None
        visible_count = len(self._get_filtered_tracks())

        if total == 0:
            self.empty_page.set_title(self._empty_title)
            self.empty_page.set_description(self._empty_description)
            self.stack.set_visible_child_name("empty")
        elif not visible:
            self.empty_page.set_title(f"No matching {self._noun}s")
            self.empty_page.set_description("Try a different search.")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("tracks")

        if visible_count == total:
            self.count_label.set_text(
                f"{total} {self._noun}{'s' if total != 1 else ''}"
            )
        else:
            self.count_label.set_text(
                f"{visible_count} of {total} {self._noun}s"
            )
