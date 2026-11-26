"""The Playing queue page: the tracks that are playing now and the ones that
will play after, in the order of the queue.

Several tracks can be selected, with the same keys as in the other lists
(Ctrl+click, Shift+click, Ctrl+A). A double click plays a track of the queue
from there. The right-click menu acts on the selection: it can play the first
selected track, move the selected ones to right after the current track, take
them out of the queue, and do what the other lists do with tracks (playlists,
favorites, album, artist, artwork, deletion).

The track being played is highlighted, like in the other lists, and cannot be
removed. "Clear" empties the queue of everything after it.

The queue only means something while a track is loaded (with nothing playing
it is just the whole library), so the page says "Nothing is playing" then.
With shuffle on, the queue is shown in its own order, not the random one it
is played in.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from melody.layout import PAGE_MARGIN
from melody.services.collection_actions import (
    attach_context_menu,
    delete_tracks,
    run_tracks_action,
    scroll_to_row,
)
from melody.widgets.track_table import (
    create_track_row,
    update_favorite_in_list,
    update_play_count_in_list,
)

THUMBNAIL_SIZE = 32
COLUMNS = ("title", "album", "artist", "duration")


class QueueView(Gtk.Box):
    def __init__(self, playback_manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self._title = "Playing queue"
        self.playback_manager = playback_manager
        # What the list was last built from.
        self._signature = None
        self._shown_track = None

        self._build_header()
        self._build_list()

        self.append(self.hint)
        self.append(self.stack)

        # The queue changes from everywhere (menus, the player bar, the end
        # of a track...): the page looks for changes while it is on screen.
        GLib.timeout_add(250, self._poll)

    @property
    def title(self):
        return self._title

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

        self.clear_button = Gtk.Button(label="Clear")
        self.clear_button.set_tooltip_text(
            "Take everything after the current track out of the queue"
        )
        self.clear_button.set_sensitive(False)
        self.clear_button.connect("clicked", self._on_clear_clicked)

        header.append(title)
        header.append(self.count_label)
        header.append(self.clear_button)
        self.append(header)

        self.hint = Gtk.Label(
            label=(
                "Shuffle is on: the tracks after the current one are "
                "played in a random order."
            ),
            xalign=0,
        )
        self.hint.add_css_class("dim-label")
        self.hint.add_css_class("caption")
        self.hint.set_margin_start(PAGE_MARGIN)
        self.hint.set_margin_end(PAGE_MARGIN)
        self.hint.set_visible(False)

    def _build_list(self):
        self.list = Gtk.ListBox()
        # Several tracks can be selected, as in the other lists.
        self.list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.list.set_activate_on_single_click(False)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row_activated)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.scrolled.set_margin_start(PAGE_MARGIN)
        self.scrolled.set_margin_end(PAGE_MARGIN)
        self.scrolled.set_margin_top(8)
        self.scrolled.set_margin_bottom(16)
        self.scrolled.set_child(self.list)

        self.message = Adw.StatusPage(
            title="Nothing is playing",
            description="Play a track to see the queue here.",
            icon_name="view-list-ordered-symbolic",
        )
        self.message.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.message, "message")
        self.stack.add_named(self.scrolled, "queue")
        self.stack.set_visible_child_name("message")

    # ------------------------------------------------------------------
    # Keeping up with the queue
    # ------------------------------------------------------------------

    def _poll(self):
        manager = self.playback_manager

        if self.get_mapped():
            signature = self._current_signature()

            if signature != self._signature:
                self._signature = signature
                self._rebuild()

            self._update_controls()

        return GLib.SOURCE_CONTINUE

    def _current_signature(self):
        manager = self.playback_manager

        return (
            manager.queue_version,
            manager.current_index,
            id(manager.current_track),
        )

    def _is_loaded(self):
        manager = self.playback_manager

        return (
            manager.current_track is not None
            and 0 <= manager.current_index < len(manager.queue)
        )

    def _update_controls(self):
        manager = self.playback_manager
        upcoming = (
            len(manager.queue) - manager.current_index - 1
            if self._is_loaded() else 0
        )

        self.clear_button.set_sensitive(upcoming > 0)
        self.hint.set_visible(bool(manager.shuffle) and self._is_loaded())

    def _rebuild(self):
        manager = self.playback_manager

        # Where the list was, to put it back after the rows are made again.
        adjustment = self.scrolled.get_vadjustment()
        position = adjustment.get_value()

        # The selection is kept too, by queue position.
        selected = {row.queue_index for row in self.list.get_selected_rows()}

        child = self.list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.list.remove(child)
            child = next_child

        if not self._is_loaded():
            self._shown_track = None
            self.count_label.set_text("")
            self.stack.set_visible_child_name("message")
            return

        current_row = None
        rows = {}

        for index, track in enumerate(manager.queue):
            row = self._create_row(index, track)
            self.list.append(row)
            rows[index] = row

            if index == manager.current_index:
                current_row = row

        for index in selected:
            if index in rows:
                self.list.select_row(rows[index])

        upcoming = len(manager.queue) - manager.current_index - 1
        self.count_label.set_text(
            f"{upcoming} up next" if upcoming else "Nothing up next"
        )
        self.stack.set_visible_child_name("queue")

        if self._shown_track != id(manager.current_track):
            # Another track is playing: show it.
            self._shown_track = id(manager.current_track)
            scroll_to_row(current_row)
        else:
            # An edit of the queue: stay where the person was looking.
            def restore():
                adjustment.set_value(position)
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(60, restore)

    def _create_row(self, index, track):
        artwork = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        artwork.set_pixel_size(round(THUMBNAIL_SIZE * 0.7))
        artwork.set_size_request(THUMBNAIL_SIZE, THUMBNAIL_SIZE)

        artwork_path = track.get("artwork_path")

        if artwork_path and Path(artwork_path).is_file():
            artwork.set_from_file(artwork_path)
            artwork.set_pixel_size(THUMBNAIL_SIZE)

        row = create_track_row(
            track,
            columns=COLUMNS,
            artwork=artwork,
            vertical_margin=6,
        )
        row.queue_index = index

        attach_context_menu(
            row,
            lambda row=row: self._menu_entries(row),
            keep_selection=True,
        )
        return row

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _selection_for_menu(self, row):
        """The rows the menu of ``row`` acts on, in queue order. A row that is
        part of the selection keeps the selection; any other row is selected
        on its own first (as in the other lists)."""
        if not row.is_selected():
            self.list.unselect_all()
            self.list.select_row(row)

        return sorted(
            self.list.get_selected_rows(),
            key=lambda selected: selected.queue_index,
        )

    def _menu_entries(self, row):
        manager = self.playback_manager
        rows = self._selection_for_menu(row)
        indexes = [selected.queue_index for selected in rows]
        tracks = [manager.queue[index] for index in indexes]
        current = manager.current_index
        upcoming = [index for index in indexes if index != current]

        entries = [("Play", lambda: manager.play_index(indexes[0]))]

        # The track being played stays where it is: only the others move or
        # leave the queue.
        if upcoming:
            entries.append(
                ("Play next", lambda: self._move_next(upcoming))
            )
            entries.append(
                ("Remove from queue", lambda: self._remove(upcoming))
            )

        entries.append(None)
        entries.append((
            "Add to playlist\u2026",
            lambda: run_tracks_action("add_to_playlist", tracks),
        ))
        entries.append((
            "Remove from favorites"
            if all(track.get("favorite") for track in tracks)
            else "Add to favorites",
            lambda: run_tracks_action("favorite", tracks),
        ))
        entries.append((
            "Set album\u2026",
            lambda: run_tracks_action("set_album", tracks),
        ))
        entries.append((
            "Set artist\u2026",
            lambda: run_tracks_action("set_artist", tracks),
        ))
        entries.append((
            "Set artwork\u2026",
            lambda: run_tracks_action("set_artwork", tracks),
        ))
        entries.append(None)
        entries.append(("Delete", lambda: delete_tracks(tracks)))

        return entries

    def _move_next(self, indexes):
        # The positions change: the selection would point at other tracks.
        self.list.unselect_all()
        self.playback_manager.play_queued_many_next(indexes)
        self._refresh_now()

    def _remove(self, indexes):
        self.list.unselect_all()
        self.playback_manager.remove_many_from_queue(indexes)
        self._refresh_now()

    def _refresh_now(self):
        """Build the list again at once, rather than on the next poll: the rows
        of the old queue must be gone before the menu's selection is restored."""
        self._signature = self._current_signature()
        self._rebuild()

    def _on_row_activated(self, list_box, row):
        self.playback_manager.play_index(row.queue_index)

    def _on_clear_clicked(self, button):
        self.playback_manager.clear_upcoming()

    # ------------------------------------------------------------------
    # What the window asks every page
    # ------------------------------------------------------------------

    def play_page(self):
        """The player's Play button has nothing to start from this page."""
        return False

    def update_play_count(self, track_id, count):
        """A track was played once more: update its row."""
        update_play_count_in_list(self.list, track_id, count)

    def update_favorite(self, track_id, favorite):
        """A track was marked or unmarked as a favorite: update its heart."""
        update_favorite_in_list(self.list, track_id, favorite)
