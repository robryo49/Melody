"""The Search page: one search bar that looks through the whole library and
lists the artists, albums, tracks and compilations it finds.

Clicking an artist or an album opens its page. A double click on a track (or
a compilation) plays it, and the right-click menu is the one every track has.
Each list shows its first ``MAX_PER_SECTION`` results, with how many there
are in all.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.layout import PAGE_MARGIN
from melody.services.collection_actions import (
    attach_track_context_menu,
    playback_source,
    toggle_track_playback,
    track_is_playing,
)
from melody.services.library_search import search_library
from melody.widgets.cover_style import install_cover_css
from melody.widgets.track_table import (
    create_track_row,
    enable_multiple_selection,
    link_selections,
    update_favorite_in_list,
)

MAX_PER_SECTION = 50
SEARCH_BAR_WIDTH = 360
THUMBNAIL_SIZE = 40
TRACK_COLUMNS = ("title", "album", "artist", "duration")


class SearchView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        install_cover_css()

        self._title = "Search"
        self.tracks = []
        self.playback_manager = None
        self._edit_track = None
        self._choose_playlist = None
        self._open_artist = None
        self._open_album = None
        # The result lists of the tracks and compilations showing, so a
        # favorite change can reach their hearts.
        self._track_lists = []
        # What the search found: the lists a track is played from.
        self._results = search_library([], "")

        self._build_header()
        self._build_results()

        self.append(self.stack)
        self._refresh()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def title(self):
        return self._title

    def set_playback_manager(self, playback_manager):
        self.playback_manager = playback_manager

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist
        self._refresh()

    def set_navigation(self, open_artist, open_album):
        """``open_artist(track)`` / ``open_album(track)`` show the page of the
        artist / album a track belongs to."""
        self._open_artist = open_artist
        self._open_album = open_album

    def set_tracks(self, tracks):
        self.tracks = list(tracks)
        self._refresh()

    def focus_search(self):
        """Put the cursor in the search bar (done when the page is shown)."""
        def focus():
            self.search_entry.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(focus)

    def update_favorite(self, track_id, favorite):
        """A track was marked or unmarked as a favorite: update its heart."""
        for track_list in self._track_lists:
            update_favorite_in_list(track_list, track_id, favorite)

    def play_page(self):
        """Play the tracks found, from the first. False when there are none."""
        tracks = self._results["tracks"] or self._results["compilations"]

        if self.playback_manager is None or not tracks:
            return False

        self.playback_manager.play_queue(
            tracks,
            0,
            playback_source("list", self._title, tracks),
        )
        return True

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(PAGE_MARGIN)
        header.set_margin_end(PAGE_MARGIN)
        header.set_margin_top(16)

        title = Gtk.Label(label=self._title, xalign=0)
        title.add_css_class("title-2")

        self.count_label = Gtk.Label(label="")
        self.count_label.add_css_class("dim-label")
        self.count_label.set_hexpand(True)
        self.count_label.set_xalign(0)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            "Search artists, albums, tracks…"
        )
        self.search_entry.set_size_request(SEARCH_BAR_WIDTH, -1)
        self.search_entry.set_hexpand(False)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("stop-search", self._on_stop_search)

        header.append(title)
        header.append(self.count_label)
        header.append(self.search_entry)
        self.append(header)

    def _build_results(self):
        self.results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
        )

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_margin_start(PAGE_MARGIN)
        scrolled.set_margin_end(PAGE_MARGIN)
        scrolled.set_margin_top(8)
        scrolled.set_margin_bottom(16)
        scrolled.set_child(self.results_box)
        self.scrolled = scrolled

        self.message = Adw.StatusPage()
        self.message.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.message, "message")
        self.stack.add_named(scrolled, "results")

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------

    def _on_search_changed(self, _entry):
        self._refresh()

    def _on_stop_search(self, _entry):
        # Escape empties the search bar.
        self.search_entry.set_text("")

    def _clear_results(self):
        child = self.results_box.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child

        self._track_lists = []

    def _refresh(self):
        query = self.search_entry.get_text().strip()
        self._results = search_library(self.tracks, query)
        self._clear_results()

        total = sum(len(found) for found in self._results.values())

        if not query:
            self.count_label.set_text("")
            self._show_message(
                "system-search-symbolic",
                "Search your library",
                "Find artists, albums, tracks and compilations.",
            )
            return

        if total == 0:
            self.count_label.set_text("")
            self._show_message(
                "system-search-symbolic",
                "No results",
                "Try different words.",
            )
            return

        self.count_label.set_text(f"{total} result{'s' if total != 1 else ''}")

        artists = self._results["artists"]
        albums = self._results["albums"]
        tracks = self._results["tracks"]
        compilations = self._results["compilations"]

        if artists:
            self._add_section(
                "Artists",
                len(artists),
                self._entry_list(
                    artists[:MAX_PER_SECTION],
                    lambda item: self._create_artist_row(item),
                ),
            )

        if albums:
            self._add_section(
                "Albums",
                len(albums),
                self._entry_list(
                    albums[:MAX_PER_SECTION],
                    lambda item: self._create_album_row(item),
                ),
            )

        if tracks:
            self._add_section(
                "Tracks",
                len(tracks),
                self._track_list(tracks[:MAX_PER_SECTION], tracks),
            )

        if compilations:
            self._add_section(
                "Compilations",
                len(compilations),
                self._track_list(
                    compilations[:MAX_PER_SECTION],
                    compilations,
                ),
            )

        self.scrolled.get_vadjustment().set_value(0)
        self.stack.set_visible_child_name("results")

        # Tracks and compilations are two lists: one selection at a time.
        if len(self._track_lists) > 1:
            link_selections(*self._track_lists)

    def _show_message(self, icon, heading, description):
        self.message.set_icon_name(icon)
        self.message.set_title(heading)
        self.message.set_description(description)
        self.stack.set_visible_child_name("message")

    def _add_section(self, name, total, content):
        """A titled list, with how many results it has in all."""
        heading = Gtk.Label(label=name, xalign=0)
        heading.add_css_class("title-4")

        shown = min(total, MAX_PER_SECTION)
        count = Gtk.Label(
            label=(
                str(total) if shown == total
                else f"first {shown} of {total}"
            ),
            xalign=0,
        )
        count.add_css_class("dim-label")

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.append(heading)
        title_row.append(count)

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.append(title_row)
        section.append(content)
        self.results_box.append(section)

    # ------------------------------------------------------------------
    # Artists and albums
    # ------------------------------------------------------------------

    def _entry_list(self, items, create_row):
        """A list of artists or albums: one click opens the page."""
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.set_activate_on_single_click(True)
        listbox.add_css_class("boxed-list")
        listbox.connect("row-activated", lambda _list, row: row.on_open())

        for item in items:
            listbox.append(create_row(item))

        return listbox

    def _create_entry_row(
            self,
            title,
            subtitle,
            artwork_path,
            placeholder_icon,
            round_cover,
            on_open,
    ):
        row = Gtk.ListBoxRow()
        row.on_open = on_open

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        thumbnail = Gtk.Image()
        thumbnail.set_size_request(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        thumbnail.add_css_class("round-cover" if round_cover else "cover-art")

        if artwork_path and Path(artwork_path).is_file():
            thumbnail.set_from_file(artwork_path)
            thumbnail.set_pixel_size(THUMBNAIL_SIZE)
        else:
            thumbnail.set_from_icon_name(placeholder_icon)
            thumbnail.set_pixel_size(round(THUMBNAIL_SIZE * 0.6))
            thumbnail.add_css_class("cover-placeholder")

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.set_valign(Gtk.Align.CENTER)
        labels.set_hexpand(True)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.add_css_class("heading")

        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle_label.add_css_class("dim-label")
        subtitle_label.add_css_class("caption")

        labels.append(title_label)
        labels.append(subtitle_label)

        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")

        content.append(thumbnail)
        content.append(labels)
        content.append(arrow)
        row.set_child(content)
        return row

    def _create_artist_row(self, artist):
        albums = artist["album_count"]
        tracks = artist["track_count"]
        parts = []

        if albums:
            parts.append(f"{albums} album{'s' if albums != 1 else ''}")

        parts.append(f"{tracks} track{'s' if tracks != 1 else ''}")

        # The artist page is found by the artist's name, whatever the track
        # used as an example is credited to.
        example = dict(artist["track"], artist=artist["name"])

        return self._create_entry_row(
            artist["name"],
            " · ".join(parts),
            artist["artwork_path"],
            "avatar-default-symbolic",
            True,
            lambda: self._open_artist and self._open_artist(example),
        )

    def _create_album_row(self, album):
        count = album["track_count"]

        return self._create_entry_row(
            album["title"],
            f"{album['artist']} · {count} track{'s' if count != 1 else ''}",
            album["artwork_path"],
            "media-optical-symbolic",
            False,
            lambda: self._open_album and self._open_album(album["track"]),
        )

    # ------------------------------------------------------------------
    # Tracks and compilations
    # ------------------------------------------------------------------

    def _track_list(self, shown, queue):
        """A list of tracks. ``queue`` is every track of the section, the one
        a double click plays from (not just the ones shown)."""
        source = playback_source("list", self._title, queue)

        listbox = Gtk.ListBox()
        # Several tracks can be selected (Ctrl or Shift + click) to act on
        # them together, from their right-click menu.
        enable_multiple_selection(listbox)
        listbox.set_activate_on_single_click(False)
        listbox.add_css_class("boxed-list")

        def on_activated(_list, row):
            if self.playback_manager is None:
                return

            for index, track in enumerate(queue):
                if track.get("id") == row.track.get("id"):
                    self.playback_manager.play_queue(queue, index, source)
                    return

        listbox.connect("row-activated", on_activated)

        for track in shown:
            listbox.append(self._create_track_row(track, queue, source))

        self._track_lists.append(listbox)
        return listbox

    def _create_track_row(self, track, queue, source):
        thumbnail = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        thumbnail.set_pixel_size(round(THUMBNAIL_SIZE * 0.5))
        thumbnail.set_size_request(32, 32)

        artwork_path = track.get("artwork_path")

        if artwork_path and Path(artwork_path).is_file():
            thumbnail.set_from_file(artwork_path)
            thumbnail.set_pixel_size(32)

        row = create_track_row(
            track,
            columns=TRACK_COLUMNS,
            artwork=thumbnail,
            vertical_margin=6,
        )

        if self._edit_track is not None:
            attach_track_context_menu(
                row,
                track,
                on_play_pause=lambda t: toggle_track_playback(
                    self.playback_manager, t, queue, source,
                ),
                is_playing=lambda t: track_is_playing(self.playback_manager, t),
                on_edit=self._edit_track,
                on_add_to_playlist=self._choose_playlist,
            )

        return row
