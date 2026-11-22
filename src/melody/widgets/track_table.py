"""The table look shared by every list of tracks.

A header with the column names (clickable, to sort by that column, ascending
or descending) sits above the rows, and the values in the rows line up under
it, aligned to the left of their column.

Columns share the width of the list: Title, Album and Artist grow and shrink
with the window, Play Count and Duration keep a fixed width. The header and
the rows use the same layout, so they always line up.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk, Pango

from melody.services.track_range import track_length

# Gap between two columns, and the space between the edge of a row and its
# first column (the header uses the same two, to line up with the rows).
COLUMN_SPACING = 12
ROW_MARGIN = 12

# Width of the thumbnail column of the lists that show a cover.
ARTWORK_WIDTH = 32

DEFAULT_COLUMNS = ("title", "album", "artist", "play_count", "duration")

# Inside an album every track has the same album (and nearly always the same
# artist), so those two columns are left out.
ALBUM_COLUMNS = ("title", "play_count", "duration")

# key: (header text, weight, width, fixed). A fixed column is exactly
# ``width`` wide; the others share what is left in proportion to ``weight``
# and never get narrower than ``width``.
_COLUMNS = {
    "title": ("Title", 5, 100, False),
    "album": ("Album", 3, 64, False),
    "artist": ("Artist", 3, 64, False),
    "play_count": ("Play Count", 0, 92, True),
    "duration": ("Duration", 0, 90, True),
}

# Columns whose first click sorts the biggest value first.
_DESCENDING_FIRST = {"play_count"}

_CSS = """
.track-header-cell {
    padding: 4px 0;
}
.track-header-cell label,
.track-header-cell image {
    opacity: 0.65;
}
.track-header-cell:hover label,
.track-header-cell:hover image,
.track-header-cell.sorted label,
.track-header-cell.sorted image {
    opacity: 1;
}
.track-heart {
    opacity: 0;
    font-size: 1.15em;
}
row:hover .track-heart,
.track-heart.favorite {
    opacity: 1;
}
"""
_css_installed = False


def _install_css():
    global _css_installed

    if _css_installed:
        return

    display = Gdk.Display.get_default()

    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_installed = True


def format_duration(duration):
    duration = max(0, int(duration or 0))
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"


def _cell_text(track, key):
    if key == "title":
        return str(track.get("title") or "Unknown Title")

    if key == "album":
        return str(track.get("album") or "")

    if key == "artist":
        return str(track.get("artist") or "Unknown Artist")

    if key == "play_count":
        return str(int(track.get("play_count") or 0))

    return format_duration(track_length(track))


# ----------------------------------------------------------------------
# Sorting
# ----------------------------------------------------------------------

_SORT_KEYS = {
    "title": lambda track: str(track.get("title") or "").casefold(),
    "album": lambda track: str(track.get("album") or "").casefold(),
    "artist": lambda track: str(track.get("artist") or "").casefold(),
    "play_count": lambda track: int(track.get("play_count") or 0),
    "duration": lambda track: track_length(track),
}


class TrackSort:
    """What a list is sorted by (a column) and in which direction."""

    def __init__(self, key="title", descending=False):
        self.key = key
        self.descending = descending

    def toggle(self, key):
        """A click on a column: the same column again turns the order around,
        another column sorts by it."""
        if key == self.key:
            self.descending = not self.descending
        else:
            self.key = key
            self.descending = key in _DESCENDING_FIRST

    def sort(self, tracks):
        """``tracks`` in this order. Tracks that tie keep title order."""
        by_title = sorted(tracks, key=_SORT_KEYS["title"])

        return sorted(
            by_title,
            key=_SORT_KEYS.get(self.key, _SORT_KEYS["title"]),
            reverse=self.descending,
        )


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------

class _ColumnsLayout(Gtk.LayoutManager):
    """Puts the children of a box side by side, one per column."""

    def __init__(self, cells):
        super().__init__()
        # One ``(fixed width or None, weight, minimum width)`` per child.
        self._cells = list(cells)

    def _widths(self, width):
        count = len(self._cells)
        available = width - COLUMN_SPACING * max(0, count - 1)
        fixed_total = sum(
            fixed for fixed, _weight, _minimum in self._cells
            if fixed is not None
        )
        total_weight = sum(
            weight for fixed, weight, _minimum in self._cells
            if fixed is None
        ) or 1
        shared = max(0, available - fixed_total)

        return [
            fixed if fixed is not None
            else max(minimum, shared * weight / total_weight)
            for fixed, weight, minimum in self._cells
        ]

    def do_measure(self, widget, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            minimum = sum(
                fixed if fixed is not None else least
                for fixed, _weight, least in self._cells
            ) + COLUMN_SPACING * max(0, len(self._cells) - 1)

            return minimum, minimum, -1, -1

        minimum = natural = 0
        child = widget.get_first_child()

        while child is not None:
            if child.should_layout():
                child_min, child_nat, _, _ = child.measure(
                    Gtk.Orientation.VERTICAL,
                    -1,
                )
                minimum = max(minimum, child_min)
                natural = max(natural, child_nat)

            child = child.get_next_sibling()

        return minimum, natural, -1, -1

    def do_allocate(self, widget, width, height, baseline):
        children = []
        child = widget.get_first_child()

        while child is not None:
            if child.should_layout():
                children.append(child)

            child = child.get_next_sibling()

        position = 0.0

        for child, column_width in zip(children, self._widths(width)):
            left = round(position)
            right = round(position + column_width)

            area = Gdk.Rectangle()
            area.x = left
            area.y = 0
            area.width = max(0, right - left)
            area.height = height
            child.size_allocate(area, -1)

            position += column_width + COLUMN_SPACING


def _cell_for(key):
    _title, weight, width, fixed = _COLUMNS[key]

    return (width, 0, width) if fixed else (None, weight, width)


def _artwork_cell():
    return (ARTWORK_WIDTH, 0, ARTWORK_WIDTH)


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------

class TrackTableHeader(Gtk.Box):
    """The column names above a list of tracks.

    Clicking a name sorts ``sort`` (a ``TrackSort``) by that column, or turns
    its order around if the list is already sorted by it, then calls
    ``on_changed()`` so the list can be built again. Put it above the
    scrolled window holding the rows, not inside it, to keep it in view while
    scrolling.

    ``margin_start`` and ``margin_end`` are those of the list below it.
    """

    def __init__(
            self,
            sort,
            on_changed,
            *,
            columns=DEFAULT_COLUMNS,
            with_artwork=False,
            margin_start=0,
            margin_end=0,
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        _install_css()

        self._sort = sort
        self._on_changed = on_changed
        self._cells = {}

        self.set_margin_start(margin_start + ROW_MARGIN)
        self.set_margin_end(margin_end + ROW_MARGIN)

        cells = []

        if with_artwork:
            # Nothing to name above the thumbnails.
            self.append(Gtk.Box())
            cells.append(_artwork_cell())

        for key in columns:
            self.append(self._create_cell(key))
            cells.append(_cell_for(key))

        self.set_layout_manager(_ColumnsLayout(cells))
        self.refresh()

    def _create_cell(self, key):
        cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cell.add_css_class("track-header-cell")
        cell.set_cursor_from_name("pointer")

        label = Gtk.Label(label=_COLUMNS[key][0], xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("caption-heading")

        arrow = Gtk.Image()
        arrow.set_pixel_size(12)
        arrow.set_visible(False)

        cell.append(label)
        cell.append(arrow)

        gesture = Gtk.GestureClick.new()
        gesture.set_button(Gdk.BUTTON_PRIMARY)
        gesture.connect(
            "pressed",
            lambda _gesture, _n_press, _x, _y, column=key: self._clicked(column),
        )
        cell.add_controller(gesture)

        self._cells[key] = (cell, arrow)
        return cell

    def _clicked(self, key):
        self._sort.toggle(key)
        self.refresh()
        self._on_changed()

    def refresh(self):
        """Show which column the list is sorted by, and in which direction."""
        for key, (cell, arrow) in self._cells.items():
            sorted_by_this = key == self._sort.key

            arrow.set_visible(sorted_by_this)
            arrow.set_from_icon_name(
                "pan-down-symbolic"
                if self._sort.descending
                else "pan-up-symbolic"
            )

            if sorted_by_this:
                cell.add_css_class("sorted")
            else:
                cell.remove_css_class("sorted")


# ----------------------------------------------------------------------
# Favorites
# ----------------------------------------------------------------------

# What a click on a row's heart does: ``handler(track)`` toggles the track's
# favorite mark. Set once by the window, so the rows need no database.
_favorite_handler = None


def set_favorite_handler(handler):
    global _favorite_handler
    _favorite_handler = handler


def toggle_favorite(track):
    """Mark ``track`` as a favorite, or take the mark away."""
    if _favorite_handler is not None:
        _favorite_handler(track)


def _show_favorite(heart, favorite):
    """A filled heart for a favorite, an outline for any other track (which
    only shows while the row is hovered)."""
    heart.set_text("\u2665" if favorite else "\u2661")

    if favorite:
        heart.add_css_class("favorite")
    else:
        heart.remove_css_class("favorite")


def _create_heart(track):
    heart = Gtk.Label()
    heart.add_css_class("track-heart")
    # A little away from the right edge of the column, to look balanced
    # between the duration and the edge of the row.
    heart.set_margin_end(14)
    heart.set_cursor_from_name("pointer")
    _show_favorite(heart, bool(track.get("favorite")))

    def on_pressed(gesture, _n_press, _x, _y):
        # Keep the row from also being selected or played by this click.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        toggle_favorite(track)

    gesture = Gtk.GestureClick.new()
    gesture.set_button(Gdk.BUTTON_PRIMARY)
    gesture.connect("pressed", on_pressed)
    heart.add_controller(gesture)

    return heart


def update_favorite_in_list(list_box, track_id, favorite):
    """A track was marked or unmarked as a favorite: update its row."""
    child = list_box.get_first_child()

    while child is not None:
        track = getattr(child, "track", None)

        if track is not None and track.get("id") == track_id:
            track["favorite"] = 1 if favorite else 0
            heart = getattr(child, "favorite_label", None)

            if heart is not None:
                _show_favorite(heart, favorite)

        child = child.get_next_sibling()


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------

def create_track_row(
        track,
        *,
        columns=DEFAULT_COLUMNS,
        artwork=None,
        vertical_margin=8,
):
    """A list row showing ``track`` in the table's columns.

    ``artwork`` is an optional widget (a thumbnail) for a first column of
    ``ARTWORK_WIDTH``, for the lists whose header was made ``with_artwork``.
    The row carries its track as ``row.track``.
    """
    row = Gtk.ListBoxRow()
    row.track = track

    cells = []
    widgets = []

    if artwork is not None:
        widgets.append(artwork)
        cells.append(_artwork_cell())

    for key in columns:
        label = Gtk.Label(label=_cell_text(track, key), xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        if key != "title":
            label.add_css_class("dim-label")
            label.add_css_class("caption")

        if key == "play_count":
            row.play_count_label = label

        if key == "duration":
            # The duration is mostly empty space (the column is as wide as
            # its name), so the heart goes in the same cell, at its right
            # end.
            label.set_hexpand(True)
            heart = _create_heart(track)
            row.favorite_label = heart

            cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            cell.append(label)
            cell.append(heart)
            widgets.append(cell)
        else:
            widgets.append(label)

        cells.append(_cell_for(key))

    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    content.set_layout_manager(_ColumnsLayout(cells))
    content.set_margin_start(ROW_MARGIN)
    content.set_margin_end(ROW_MARGIN)
    content.set_margin_top(vertical_margin)
    content.set_margin_bottom(vertical_margin)

    for widget in widgets:
        content.append(widget)

    row.set_child(content)
    return row


def update_play_count_in_list(list_box, track_id, count):
    """A track was played once more: update its row in ``list_box``."""
    child = list_box.get_first_child()

    while child is not None:
        track = getattr(child, "track", None)

        if track is not None and track.get("id") == track_id:
            track["play_count"] = count
            label = getattr(child, "play_count_label", None)

            if label is not None:
                label.set_text(str(count))

        child = child.get_next_sibling()
