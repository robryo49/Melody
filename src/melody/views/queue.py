"""The Playing queue page: the tracks that are playing now and the ones that
will play after, in the order of the queue.

A double click plays a track of the queue from there, and its right-click
menu can play it, move it to right after the current track, or take it out of
the queue. The track being played is marked with a play (or pause) icon where
the cover would be, and cannot be removed. "Clear" empties the queue of
everything after it.

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
        # What the list was last built from, and the playback state its
        # "now playing" icon shows.
        self._signature = None
        self._state = None
        self._shown_track = None
        self._now_icon = None

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
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
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
            signature = (
                manager.queue_version,
                manager.current_index,
                id(manager.current_track),
            )

            if signature != self._signature:
                self._signature = signature
                self._rebuild()

            if manager.state != self._state:
                self._state = manager.state
                self._update_now_icon()

            self._update_controls()

        return GLib.SOURCE_CONTINUE

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

    def _update_now_icon(self):
        if self._now_icon is None:
            return

        self._now_icon.set_from_icon_name(
            "media-playback-start-symbolic"
            if self.playback_manager.state == "playing"
            else "media-playback-pause-symbolic"
        )
        self._now_icon.set_pixel_size(16)

    def _rebuild(self):
        manager = self.playback_manager

        # Where the list was, to put it back after the rows are made again.
        adjustment = self.scrolled.get_vadjustment()
        position = adjustment.get_value()

        child = self.list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.list.remove(child)
            child = next_child

        self._now_icon = None

        if not self._is_loaded():
            self._shown_track = None
            self.count_label.set_text("")
            self.stack.set_visible_child_name("message")
            return

        current_row = None

        for index, track in enumerate(manager.queue):
            row = self._create_row(
                index,
                track,
                index == manager.current_index,
            )
            self.list.append(row)

            if index == manager.current_index:
                current_row = row

        upcoming = len(manager.queue) - manager.current_index - 1
        self.count_label.set_text(
            f"{upcoming} up next" if upcoming else "Nothing up next"
        )
        self.stack.set_visible_child_name("queue")
        self._update_now_icon()

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

    def _create_row(self, index, track, is_current):
        artwork = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        artwork.set_pixel_size(round(THUMBNAIL_SIZE * 0.7))
        artwork.set_size_request(THUMBNAIL_SIZE, THUMBNAIL_SIZE)

        if is_current:
            # The track being played shows what it is doing instead of its
            # cover (see ``_update_now_icon``).
            self._now_icon = artwork
        else:
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

        attach_context_menu(row, lambda row=row: self._menu_entries(row))
        return row

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _menu_entries(self, row):
        manager = self.playback_manager
        index = row.queue_index

        # Right-clicking a track selects it, like in the other lists.
        self.list.select_row(row)

        entries = [("Play", lambda: manager.play_index(index))]

        if index != manager.current_index:
            entries.append(
                ("Play next", lambda: manager.play_queued_next(index))
            )
            entries.append(
                ("Remove from queue", lambda: manager.remove_from_queue(index))
            )

        return entries

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
