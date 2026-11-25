import threading
from collections import Counter
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from melody.services.artwork import get_cached_own_artwork, image_as_png
from melody.widgets.track_table import selected_tracks, toggle_favorite
from melody.services.compilations import (
    automatic_is_compilation,
    format_clock,
    is_compilation,
)
from melody.services.playlist_categories import category_suggestions
from melody.services.settings import get_settings
from melody.services.replaygain import read_track_gain, supports_replaygain
from melody.services.singles import has_album
from melody.services.track_range import track_length
from melody.services.image_sources import (
    album_search,
    artist_search,
    playlist_search,
    track_search,
)
from melody.widgets.cover_style import install_cover_css
from melody.widgets.gain_slider import GainSlider
from melody.widgets.image_picker import show_image_picker, texture_from_bytes
from melody.widgets.range_selector import TimeRangeSelector


SORT_OPTIONS = ["Title", "Album", "Artist", "Duration", "Play Count"]

# An album page has no use for sorting by album or artist (they are all the
# same there): it only offers these, whose positions map to ``SORT_OPTIONS``'.
ALBUM_SORT_OPTIONS = ["Title", "Duration", "Play Count"]
_ALBUM_SORT_INDEXES = (0, 3, 4)


def sort_album_tracks(tracks, selected_index):
    """``sort_tracks`` for the ``ALBUM_SORT_OPTIONS`` selector."""
    if selected_index not in range(len(_ALBUM_SORT_INDEXES)):
        selected_index = 0

    return sort_tracks(tracks, _ALBUM_SORT_INDEXES[selected_index])


def sort_tracks(tracks, selected_index):
    keys = {
        0: lambda track: str(track.get("title") or "").casefold(),
        1: lambda track: str(track.get("album") or "").casefold(),
        2: lambda track: str(track.get("artist") or "").casefold(),
        3: lambda track: track_length(track),
        # Most played first.
        4: lambda track: (
            -(track.get("play_count") or 0),
            str(track.get("title") or "").casefold(),
        ),
    }
    return sorted(tracks, key=keys.get(selected_index, keys[0]))


def show_text_dialog(parent, heading, description, initial_text, callback):
    dialog = Adw.MessageDialog.new(parent, heading, description)
    entry = Gtk.Entry()
    entry.set_text(initial_text or "")
    entry.set_activates_default(True)
    entry.set_hexpand(True)
    dialog.set_extra_child(entry)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance(
        "save",
        Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def on_response(dialog, response):
        if response == "save":
            value = entry.get_text().strip()
            if value:
                callback(value)

    dialog.connect("response", on_response)
    dialog.present()


def show_merge_confirmation(parent, collection_type, name, callback):
    dialog = Adw.MessageDialog.new(
        parent,
        f"Merge this {collection_type}?",
        (
            f'"{name}" already exists. This action will merge this '
            f"{collection_type} with the existing one."
        ),
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", "Confirm")
    dialog.set_response_appearance(
        "confirm",
        Adw.ResponseAppearance.DESTRUCTIVE,
    )
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(dialog, response):
        if response == "confirm":
            callback()

    dialog.connect("response", on_response)
    dialog.present()


def show_error_dialog(parent, heading, message):
    dialog = Adw.MessageDialog.new(parent, heading, str(message))
    dialog.add_response("close", "Close")
    dialog.set_default_response("close")
    dialog.present()


def scroll_to_row(row, attempts=15):
    """Scroll the page ``row`` is on so that the row sits in the middle of the
    view.

    The row may be on a page that was only just shown, and so not laid out
    yet: this waits for that (a few tries, a moment apart) before moving.
    """
    remaining = [attempts]

    def attempt():
        scrolled = row.get_ancestor(Gtk.ScrolledWindow)

        if scrolled is None:
            return GLib.SOURCE_REMOVE

        content = scrolled.get_child()

        if isinstance(content, Gtk.Viewport):
            content = content.get_child()

        adjustment = scrolled.get_vadjustment()
        page = adjustment.get_page_size()
        found, bounds = row.compute_bounds(content)

        if not found or row.get_height() <= 0 or page <= 0:
            remaining[0] -= 1
            return (
                GLib.SOURCE_CONTINUE
                if remaining[0] > 0
                else GLib.SOURCE_REMOVE
            )

        target = bounds.get_y() - (page - bounds.get_height()) / 2
        top = adjustment.get_lower()
        bottom = max(top, adjustment.get_upper() - page)
        adjustment.set_value(max(top, min(target, bottom)))

        return GLib.SOURCE_REMOVE

    GLib.timeout_add(60, attempt)


def reveal_track_row(list_box, track):
    """Scroll to ``track``'s row in ``list_box`` (rows carry their track as
    ``row.track``). Returns the row, or None if the list does not have it."""
    track_id = track.get("id")
    child = list_box.get_first_child()

    while child is not None:
        row_track = getattr(child, "track", None)

        if row_track is not None and row_track.get("id") == track_id:
            scroll_to_row(child)
            return child

        child = child.get_next_sibling()

    return None


# ----------------------------------------------------------------------
# Playback helpers shared by the track context menus
# ----------------------------------------------------------------------

def is_current_track(playback_manager, track):
    current = playback_manager.current_track if playback_manager else None
    return current is not None and current.get("path") == track.get("path")


def track_is_playing(playback_manager, track):
    """True when ``track`` is the current track and is actually playing."""
    return (
        playback_manager is not None
        and playback_manager.state == "playing"
        and is_current_track(playback_manager, track)
    )


def toggle_track_playback(playback_manager, track, queue, source=None):
    """Pause/resume ``track`` if it is current, otherwise play it from ``queue``.

    ``source`` says what the queue is (see ``playback_source``).
    """
    if playback_manager is None:
        return

    if is_current_track(playback_manager, track):
        playback_manager.toggle_playback()
        return

    for index, candidate in enumerate(queue):
        if candidate.get("path") == track.get("path"):
            playback_manager.play_queue(queue, index, source)
            return

    playback_manager.play_track(track)


# ----------------------------------------------------------------------
# What is being played
# ----------------------------------------------------------------------
#
# An album, an artist, a playlist, the Library... only counts as playing when
# it is what the queue was started from, not just because one of its tracks
# happens to be playing. The player remembers that as its ``source``.

def playback_source(kind, key, tracks=()):
    """Describe what a queue was started from.

    ``kind`` is "album", "artist", "playlist", "list" (the Library and the
    Compilations page)... and ``key`` identifies it among those of its kind.
    The ids of ``tracks`` let it be recognised after an edit renamed it.
    """
    return {
        "kind": kind,
        "key": key,
        "ids": frozenset(track.get("id") for track in tracks),
    }


def album_key(album):
    """What identifies an album, whichever page it is shown on."""
    return (album["artist"].casefold(), album["title"].casefold())


def album_source(album):
    return playback_source("album", album_key(album), album["tracks"])


def is_playback_source(playback_manager, kind, key, tracks=()):
    """True when the queue being played was started from this album, artist,
    playlist... (``tracks`` are its tracks now).

    It is the same one by its key, or, when an edit renamed it since, because
    most of the tracks it was started with are the ones it holds now.
    """
    source = playback_manager.source if playback_manager else None

    if source is None or source["kind"] != kind:
        return False

    if source["key"] == key:
        return True

    # ``tracks`` may be a function, so they are only worked out when needed.
    if callable(tracks):
        tracks = tracks()

    ids = {track.get("id") for track in tracks}

    return len(source["ids"] & ids) * 2 > len(source["ids"])


# ----------------------------------------------------------------------
# Copying and pasting artwork
# ----------------------------------------------------------------------

_CLIPBOARD_IMAGE_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "image/gif",
)


def _clipboard():
    return Gdk.Display.get_default().get_clipboard()


def copy_image_to_clipboard(data):
    """Put the image ``data`` (any format Pillow reads) on the clipboard, at
    its full size. Raises if it is not an image."""
    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(image_as_png(data)))

    # Declared as a plain Gdk.Texture: the instance's own type is a private
    # subclass, which the clipboard would then advertise instead.
    value = GObject.Value(Gdk.Texture, texture)
    _clipboard().set_content(Gdk.ContentProvider.new_for_value(value))


def clipboard_has_image():
    """True when the clipboard holds an image (not just text or files)."""
    formats = _clipboard().get_formats()
    texture_type = Gdk.Texture.__gtype__

    if formats.contain_gtype(texture_type):
        return True

    # An image Melody put there itself may be advertised under a subclass of
    # Gdk.Texture, which contain_gtype does not match.
    try:
        gtypes = formats.get_gtypes()

        if isinstance(gtypes, tuple):
            gtypes = gtypes[0]

        if gtypes and any(gtype.is_a(texture_type) for gtype in gtypes):
            return True
    except Exception:
        pass

    return any(
        formats.contain_mime_type(mime_type)
        for mime_type in _CLIPBOARD_IMAGE_TYPES
    )


def read_clipboard_image(callback):
    """Read the image on the clipboard and call ``callback(png_bytes)`` with
    it, or ``callback(None)`` when it could not be read."""
    def done(clipboard, result):
        try:
            texture = clipboard.read_texture_finish(result)
            data = texture.save_to_png_bytes().get_data() if texture else None
        except Exception:
            data = None

        callback(data)

    _clipboard().read_texture_async(None, done)


# What a track's, album's, artist's or playlist's "Copy artwork" and "Paste
# artwork" do, registered once by the window for every menu (like the
# navigation further down). Each kind has a ``copy(item)`` and a
# ``paste(item)``.
_artwork_actions = {}


def set_artwork_actions(kind, copy, paste):
    """Register the copy and paste of ``kind``: "track", "album", "artist"
    or "playlist"."""
    _artwork_actions[kind] = (copy, paste)


def artwork_menu_entries(kind):
    """The ``(label, action)`` pairs a menu gets for ``kind``: always "Copy",
    and "Paste" too while the clipboard holds an image. ``action(item)``."""
    actions = _artwork_actions.get(kind)

    if actions is None:
        return []

    copy, paste = actions
    noun = "image" if kind == "artist" else "artwork"
    entries = [(f"Copy {noun}", copy)]

    if clipboard_has_image():
        entries.append((f"Paste {noun}", paste))

    return entries


# ----------------------------------------------------------------------
# Track dialogs
# ----------------------------------------------------------------------

def _close_on_escape(window):
    controller = Gtk.EventControllerKey()

    def on_key_pressed(_controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False

    controller.connect("key-pressed", on_key_pressed)
    window.add_controller(controller)


def _artist_suggestions(tracks, album_artists=False):
    """Every distinct artist in the library, A-Z.

    By default these are the tracks' own artists; with ``album_artists`` it
    is the album artists (falling back to the track artist) instead.
    """
    spellings = {}
    for track in tracks:
        if album_artists:
            name = str(
                track.get("album_artist") or track.get("artist") or ""
            ).strip()
        else:
            name = str(track.get("artist") or "").strip()
        if name:
            spellings.setdefault(name.casefold(), Counter())[name] += 1

    suggestions = [
        {
            "value": names.most_common(1)[0][0],
            "group": key,
        }
        for key, names in spellings.items()
    ]
    suggestions.sort(key=lambda item: item["value"].casefold())
    return suggestions


def _album_suggestions(tracks):
    """Every distinct (album artist, album) pair in the library, A-Z."""
    albums = {}
    for track in tracks:
        album = str(track.get("album") or "").strip()
        if not album:
            continue
        artist = str(
            track.get("album_artist") or track.get("artist") or ""
        ).strip()
        entry = albums.setdefault(
            (album.casefold(), artist.casefold()),
            {"album": Counter(), "artist": Counter(), "count": 0},
        )
        entry["album"][album] += 1
        entry["artist"][artist] += 1
        entry["count"] += 1

    suggestions = []
    for (_album_key, artist_key), entry in albums.items():
        artist = entry["artist"].most_common(1)[0][0]
        detail = artist
        suggestions.append({
            "value": entry["album"].most_common(1)[0][0],
            "detail": detail,
            "group": artist_key,
        })
    suggestions.sort(key=lambda item: item["value"].casefold())
    return suggestions


class SuggestionField(Gtk.Box):
    """A labelled text field that lists existing values underneath it.

    While the field has focus, the values that contain what has been typed
    are shown below it. Click one (or use the Up/Down arrows and Enter) to
    fill the field with it, or just keep typing to create a new value. A small
    status line says whether the typed value already exists.

    With ``noun=None`` it is a plain labelled field (no list, no status).
    ``priority()`` may return a lowercase group (e.g. the artist currently
    typed) whose suggestions are listed first.
    """

    MAX_LIST_HEIGHT = 200
    MAX_SUGGESTIONS = 50

    def __init__(
            self,
            label,
            noun=None,
            suggestions=(),
            initial="",
            priority=None,
            new_message=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # What the status line says about a value that does not exist yet
        # (the default speaks of the library).
        self._new_message = new_message
        self._noun = noun
        self._suggestions = list(suggestions)
        self._known = {item["value"].casefold() for item in self._suggestions}
        self._priority = priority
        self._has_focus = False
        self._dismissed = False
        self._skip_open = False
        self._applying = False
        self._changed_callbacks = []
        self._activate_callbacks = []
        self._picked_callbacks = []

        caption = Gtk.Label(label=label, xalign=0)
        caption.add_css_class("caption-heading")
        self.append(caption)

        self.entry = Gtk.Entry()
        self.entry.set_hexpand(True)
        self.entry.set_text(initial)
        self.append(self.entry)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("caption")
        self.append(self.status)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        # Nothing in the list takes keyboard focus, so the caret stays in the
        # entry while the person clicks a suggestion.
        self.listbox.set_focusable(False)
        self.listbox.connect("row-activated", self._on_row_activated)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.scrolled.set_focusable(False)
        self.scrolled.set_child(self.listbox)

        # A dropdown under the entry. It floats over the form instead of
        # pushing the fields below it down, and never takes keyboard focus.
        self.popover = Gtk.Popover()
        self.popover.set_parent(self.entry)
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        self.popover.set_has_arrow(False)
        self.popover.set_autohide(False)
        self.popover.set_focusable(False)
        self.popover.set_offset(0, 4)
        self.popover.set_child(self.scrolled)
        self.connect("destroy", self._on_destroy)

        self.entry.connect("changed", self._on_entry_changed)
        self.entry.connect("activate", self._on_entry_activate)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.entry.add_controller(keys)

        # Fires when focus enters/leaves the entry *or* anything inside this
        # field, so the list stays open while it is being used.
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus_enter)
        focus.connect("leave", self._on_focus_leave)
        self.add_controller(focus)

        self._update_status()

    # -- public ----------------------------------------------------------

    def get_text(self):
        return self.entry.get_text().strip()

    def connect_changed(self, callback):
        self._changed_callbacks.append(callback)

    def connect_activate(self, callback):
        self._activate_callbacks.append(callback)

    def connect_picked(self, callback):
        """``callback(suggestion)`` is called when a value is picked from the
        list (a click, or Enter on the highlighted one), just before the field
        takes it. ``suggestion`` is the dict the list was made from."""
        self._picked_callbacks.append(callback)

    def grab_focus(self, show_suggestions=True):
        # ``show_suggestions=False`` focuses the field without opening the
        # list (used for the dialog's initial focus).
        self._skip_open = not show_suggestions
        return self.entry.grab_focus()

    # -- list ------------------------------------------------------------

    def _rows(self):
        rows = []
        child = self.listbox.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _create_row(self, suggestion):
        # A compact single-line row: the value, and (for albums) the artist
        # in small dimmed text on the right.
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row.set_focusable(False)
        row.set_focus_on_click(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        title = Gtk.Label(label=suggestion["value"], xalign=0)
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)

        if suggestion.get("detail"):
            detail = Gtk.Label(label=suggestion["detail"], xalign=1)
            detail.set_max_width_chars(18)
            detail.set_ellipsize(Pango.EllipsizeMode.END)
            detail.add_css_class("dim-label")
            detail.add_css_class("caption")
            box.append(detail)

        row.set_child(box)
        row.value = suggestion["value"]
        row.suggestion = suggestion
        return row

    def _refresh(self):
        self._update_status()

        if self._noun is None:
            return

        text = self.get_text()
        needle = text.casefold()
        matches = [
            item for item in self._suggestions
            if needle in item["value"].casefold()
        ]

        priority = self._priority() if self._priority else None
        if priority:
            # Stable sort: priority group first, A-Z kept inside each part.
            matches.sort(key=lambda item: item.get("group") != priority)

        for row in self._rows():
            self.listbox.remove(row)
        for item in matches[:self.MAX_SUGGESTIONS]:
            self.listbox.append(self._create_row(item))

        only_exact = (
            len(matches) == 1 and matches[0]["value"].casefold() == needle
        )
        show = (
            self._has_focus
            and not self._dismissed
            and bool(matches)
            and not only_exact
        )

        if show:
            # Same width as the entry, so the dropdown lines up with it.
            width = self.entry.get_width()
            if width > 0:
                self.popover.set_size_request(width, -1)

            # Fit the height to the rows shown *now* (up to the maximum).
            natural = self.listbox.measure(Gtk.Orientation.VERTICAL, -1)[1]
            height = min(natural, self.MAX_LIST_HEIGHT)

            # A scrolled window that may show a scrollbar has a minimum
            # height of its own, which left a gap under a single row. Only
            # allow scrolling when the list is really taller than the max.
            self.scrolled.set_policy(
                Gtk.PolicyType.NEVER,
                (
                    Gtk.PolicyType.AUTOMATIC
                    if natural > self.MAX_LIST_HEIGHT
                    else Gtk.PolicyType.NEVER
                ),
            )
            self.scrolled.set_min_content_height(0)
            self.scrolled.set_max_content_height(height)
            self.scrolled.set_min_content_height(height)

            if self.popover.get_visible():
                self.popover.present()
            else:
                self.popover.popup()
        else:
            self.popover.popdown()

    def _update_status(self):
        text = self.get_text()

        if self._noun is None or not text:
            self.status.set_text("")
            return

        if text.casefold() in self._known:
            self.status.set_text(f"Existing {self._noun}")
            self.status.remove_css_class("accent")
            self.status.add_css_class("dim-label")
        else:
            self.status.set_text(
                self._new_message
                or f"New {self._noun} — it will be added to your library"
            )
            self.status.remove_css_class("dim-label")
            self.status.add_css_class("accent")

    def _apply(self, value):
        self._applying = True
        self.entry.set_text(value)
        self.entry.set_position(-1)
        self._applying = False
        self._dismissed = True
        self._refresh()

    def _pick(self, row):
        # Told first, so the listeners still see the field as it was.
        for callback in self._picked_callbacks:
            callback(row.suggestion)

        self._apply(row.value)

    def _move_selection(self, step):
        rows = self._rows()
        if not rows:
            return

        current = self.listbox.get_selected_row()
        if current is None:
            index = 0 if step > 0 else len(rows) - 1
        else:
            index = rows.index(current) + step

        if index < 0:
            self.listbox.unselect_all()
            return

        row = rows[min(index, len(rows) - 1)]
        self.listbox.select_row(row)
        self._scroll_to(row)

    def _scroll_to(self, row):
        adjustment = self.scrolled.get_vadjustment()
        allocation = row.get_allocation()
        top = allocation.y
        bottom = allocation.y + allocation.height
        page = adjustment.get_page_size()

        if top < adjustment.get_value():
            adjustment.set_value(top)
        elif bottom > adjustment.get_value() + page:
            adjustment.set_value(bottom - page)

    # -- signals ---------------------------------------------------------

    def _on_entry_changed(self, _entry):
        if not self._applying:
            # Typing again re-opens a list that was dismissed.
            self._dismissed = False
        self._refresh()
        for callback in self._changed_callbacks:
            callback()

    def _on_entry_activate(self, _entry):
        row = (
            self.listbox.get_selected_row()
            if self.popover.get_visible() else None
        )
        if row is not None:
            self._pick(row)
            return
        for callback in self._activate_callbacks:
            callback()

    def _on_row_activated(self, _listbox, row):
        self._pick(row)
        self.entry.grab_focus_without_selecting()

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if self._noun is None:
            return False

        opened = self.popover.get_visible()

        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            if not opened:
                self._dismissed = False
                self._refresh()
                return True
            self._move_selection(1 if keyval == Gdk.KEY_Down else -1)
            return True

        if keyval == Gdk.KEY_Escape and opened:
            self._dismissed = True
            self.popover.popdown()
            return True

        return False

    def _on_focus_enter(self, _controller):
        self._has_focus = True
        self._dismissed = self._skip_open
        self._skip_open = False
        # Re-rank: the list may depend on another field (e.g. the artist).
        self._refresh()

    def _on_focus_leave(self, _controller):
        self._has_focus = False
        self.popover.popdown()

    def _on_destroy(self, _widget):
        if self.popover.get_parent() is not None:
            self.popover.unparent()


def show_edit_dialog(
        parent,
        window_title,
        specs,
        callback,
        *,
        cover_path=None,
        cover_loader=None,
        placeholder_icon="audio-x-generic-symbolic",
        hint=None,
        image_search=None,
        apply_artwork=False,
        range_spec=None,
        compilation_spec=None,
        allow_unchanged=False,
        round_cover=False,
        info=None,
        replaygain_spec=None,
):
    """Open a window with a cover on the left and editable fields on the right.

    ``specs`` is a list of field descriptions, in display order::

        {"key": "artist", "label": "Artist", "initial": "...",
         "noun": "artist",            # optional: enables the suggestion list
         "suggestions": [...],        # optional
         "priority_field": "artist",  # optional: rank by another field
         "optional": True,           # optional: may be left empty
         "placeholder": "...",        # optional: shown while empty
         "new_message": "...",        # optional: what the status line says
                                      # of a value that does not exist yet
         "on_pick": pick,             # optional: pick(suggestion, fields) is
                                      # called when a suggestion is picked
                                      # (``fields``: key -> field)
         "tooltip": "...",            # optional
         "validate": check}           # optional: check(text) -> bool; the
                                      # field is outlined, and Save waits,
                                      # while it is False

    ``callback`` receives ``{key: text}`` and is only called when something
    actually changed. The cover is ``cover_path`` if it exists, otherwise
    ``cover_loader()`` is run in a thread and its result shown.

    With ``image_search`` (a function taking the fields' current values and
    returning an ``ImageSearch``, see ``services.image_sources``) an edit
    button appears over the cover while it is hovered. It opens the image
    picker; the chosen image only replaces the preview, and is handed to
    ``callback`` as ``values["image"]`` (raw bytes) when the person saves.

    With ``apply_artwork`` an "Apply artwork" toggle sits under the cover.
    When it is on at Save time, ``values["apply_to_tracks"]`` is ``True``.

    With ``range_spec`` (``{"duration": seconds, "start": seconds or None,
    "end": seconds or None}``) a start / end selector sits at the bottom of
    the window. ``values["start_time"]`` and ``values["end_time"]`` are then
    given to ``callback`` (``None`` meaning the very start / end of the track).

    With ``compilation_spec`` (``{"initial": bool, "automatic": bool,
    "kind": the track's current kind, "tooltip": text}``) a "Compilation"
    checkbox is added. ``values["kind"]`` is then given to ``callback``:
    ``None`` (decided by length), ``"track"`` or ``"compilation"``.

    With ``replaygain_spec`` (``{"initial": dB or None}``) a ReplayGain
    slider is added under the fields. ``values["replaygain"]`` is then given
    to ``callback``: the gain in dB, or ``None`` for no tag.

    With ``round_cover`` the cover is shown as a circle (an artist's
    picture).

    ``info`` is a list of ``(caption, value)`` pairs shown under the cover,
    each on one line ("Play count: 3"), for information that cannot be edited.

    Normally Save stays disabled until something has changed. With
    ``allow_unchanged`` it is enabled as soon as the fields are valid (for a
    window that creates something instead of editing it).
    """
    keys = tuple(spec["key"] for spec in specs)
    # A field marked "optional" may be left empty (a track's album, for a
    # single).
    required_keys = tuple(
        spec["key"] for spec in specs if not spec.get("optional")
    )
    original = {
        spec["key"]: str(spec.get("initial") or "").strip()
        for spec in specs
    }
    # Fields with a rule of their own (a number, say).
    validators = {
        spec["key"]: spec["validate"]
        for spec in specs
        if spec.get("validate") is not None
    }

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(window_title)
    window.set_resizable(False)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    save_button = Gtk.Button(label="Save")
    save_button.add_css_class("suggested-action")
    save_button.set_sensitive(False)
    header.pack_start(cancel_button)
    header.pack_end(save_button)
    toolbar.add_top_bar(header)

    # -- cover (left) ----------------------------------------------------

    cover_size = 220
    GAP = 24
    FORM_WIDTH = 380
    # Room for the fields and the hint. The suggestion dropdown floats over
    # the form, so it needs no room of its own. (What goes under the cover,
    # on the left, makes that column taller instead.)
    FORM_HEIGHT = max(
        cover_size,
        len(specs) * 86 - 16
        + (60 if replaygain_spec else 0)
        + (60 if hint else 0),
    )

    cover = Gtk.Picture()
    cover.set_content_fit(Gtk.ContentFit.COVER)
    cover.set_can_shrink(True)
    cover.set_size_request(cover_size, cover_size)

    placeholder = Gtk.Image.new_from_icon_name(placeholder_icon)
    # The music note of a track is drawn about 30% smaller than the other
    # placeholders, so it does not fill the cover's area.
    placeholder.set_pixel_size(
        56 if placeholder_icon == "audio-x-generic-symbolic" else 80
    )

    cover_stack = Gtk.Stack()
    cover_stack.add_css_class("card")
    cover_stack.set_overflow(Gtk.Overflow.HIDDEN)
    if round_cover:
        install_cover_css()
        cover_stack.add_css_class("round-cover")
    cover_stack.set_size_request(cover_size, cover_size)
    # Fill the square the overlay below reserves for it, instead of taking
    # the picture's own size.
    cover_stack.set_halign(Gtk.Align.FILL)
    cover_stack.set_valign(Gtk.Align.FILL)
    cover_stack.add_named(cover, "artwork")
    cover_stack.add_named(placeholder, "placeholder")
    cover_stack.set_visible_child_name("placeholder")

    closed = {"value": False}
    # The image picked in the picker, waiting for Save (raw bytes).
    pending = {"data": None}
    # Whether "Apply artwork" is switched on.
    apply_state = {"on": False}

    def on_close_request(_window):
        closed["value"] = True
        return False

    window.connect("close-request", on_close_request)

    def show_cover(path):
        # A cover that finishes loading late must not replace the picked one.
        if (
                closed["value"]
                or pending["data"] is not None
                or not path
                or not Path(path).is_file()
        ):
            return GLib.SOURCE_REMOVE
        cover.set_filename(str(path))
        cover_stack.set_visible_child_name("artwork")
        return GLib.SOURCE_REMOVE

    if cover_path and Path(cover_path).is_file():
        show_cover(cover_path)
    elif cover_loader is not None:
        def load_cover():
            try:
                found = cover_loader()
            except Exception:
                found = None
            GLib.idle_add(show_cover, found)

        threading.Thread(target=load_cover, daemon=True).start()

    # The cover sits in an overlay so the edit button can float over its
    # bottom-right corner, shown only while the cover is hovered (the same
    # way the play button is in the grid views).
    cover_overlay = Gtk.Overlay()
    cover_overlay.set_halign(Gtk.Align.START)
    cover_overlay.set_valign(Gtk.Align.START)
    cover_overlay.set_size_request(cover_size, cover_size)
    # The overlay is sized by an empty square, and the cover is laid over it:
    # an overlaid widget never changes the overlay's size, so the cover keeps
    # the same size whether it shows the default icon, a picture loaded from
    # the file or one just picked (a picture's own size used to leak through
    # and made the cover jump).
    cover_sizer = Gtk.Box()
    cover_sizer.set_size_request(cover_size, cover_size)
    cover_overlay.set_child(cover_sizer)
    cover_overlay.add_overlay(cover_stack)

    edit_image_button = None
    if image_search is not None:
        edit_image_button = Gtk.Button.new_from_icon_name(
            "document-edit-symbolic"
        )
        edit_image_button.add_css_class("circular")
        edit_image_button.add_css_class("osd")
        edit_image_button.set_halign(Gtk.Align.END)
        edit_image_button.set_valign(Gtk.Align.END)
        # Further in on a round cover, so the button stays on the picture.
        edge = 28 if round_cover else 8
        edit_image_button.set_margin_end(edge)
        edit_image_button.set_margin_bottom(edge)
        edit_image_button.set_tooltip_text("Change image")
        edit_image_button.set_visible(False)
        cover_overlay.add_overlay(edit_image_button)

        cover_motion = Gtk.EventControllerMotion.new()
        cover_motion.connect(
            "enter",
            lambda _controller, _x, _y: edit_image_button.set_visible(True),
        )
        cover_motion.connect(
            "leave",
            lambda _controller: edit_image_button.set_visible(False),
        )
        cover_overlay.add_controller(cover_motion)

    # The left column: the cover, then, when there are any, the "Apply
    # artwork" toggle, the read-only information (the play count) and the
    # "Compilation" checkbox, in that order. It is as wide as the artwork,
    # and what is in it fills that width. Only switched on does "Apply
    # artwork" do anything, and only when the window is saved, like the rest.
    cover_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    cover_column.set_halign(Gtk.Align.START)
    # As tall as the form beside it: what is under the cover (play count,
    # "Compilation") shares the room left under it evenly.
    cover_column.set_valign(Gtk.Align.FILL)
    cover_column.append(cover_overlay)

    apply_button = None
    if apply_artwork:
        apply_button = Gtk.ToggleButton(label="Apply artwork")
        apply_button.set_tooltip_text(
            "When saving, also write this artwork into every track of "
            "the album"
        )
        cover_column.append(apply_button)

    # -- fields (right) --------------------------------------------------

    fields = {}
    for spec in specs:
        priority_key = spec.get("priority_field")
        priority = (
            (lambda key=priority_key: fields[key].get_text().casefold())
            if priority_key else None
        )
        fields[spec["key"]] = SuggestionField(
            spec["label"],
            noun=spec.get("noun"),
            suggestions=spec.get("suggestions", ()),
            initial=original[spec["key"]],
            priority=priority,
            new_message=spec.get("new_message"),
        )

        if spec.get("placeholder"):
            fields[spec["key"]].entry.set_placeholder_text(spec["placeholder"])

        if spec.get("tooltip"):
            fields[spec["key"]].entry.set_tooltip_text(spec["tooltip"])

        if spec.get("on_pick"):
            fields[spec["key"]].connect_picked(
                lambda suggestion, pick=spec["on_pick"]: pick(
                    suggestion, fields,
                )
            )

    form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    form.set_vexpand(True)
    for key in keys:
        form.append(fields[key])

    hint_label = Gtk.Label(label=hint or "", xalign=0, wrap=True)
    hint_label.set_max_width_chars(1)  # wrap to the column, never widen it
    hint_label.add_css_class("dim-label")
    hint_label.add_css_class("caption")

    # The right column has a fixed size, so the fields keep the same width
    # whatever their status text or the hint says.
    right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=GAP)
    right.set_size_request(FORM_WIDTH, FORM_HEIGHT)
    right.set_hexpand(False)
    right.set_vexpand(False)
    right.append(form)

    # Read-only information, under the cover, on one line ("Play count: 3");
    # a long one wraps within the column.
    for caption_text, value_text in info or ():
        line = Gtk.Label(wrap=True, xalign=0)
        line.set_use_markup(True)
        line.set_markup(
            f"<b>{GLib.markup_escape_text(caption_text)}:</b> "
            f"{GLib.markup_escape_text(value_text)}"
        )
        # Wrap to the column, never widen it.
        line.set_max_width_chars(1)
        line.add_css_class("dim-label")
        # Each line takes an equal share of the room under the cover, and sits
        # in the middle of it.
        line.set_vexpand(True)
        line.set_valign(Gtk.Align.CENTER)
        cover_column.append(line)

    if hint:
        right.append(hint_label)

    # One spacing value for the window edges and the gap between columns.
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=GAP)
    content.set_margin_top(GAP)
    content.set_margin_bottom(GAP)
    content.set_margin_start(GAP)
    content.set_margin_end(GAP)
    content.append(cover_column)
    content.append(right)

    # Clicking anywhere that isn't a text field (the cover, the gaps, the
    # hint...) leaves the current field, so nothing is being edited.
    def on_content_pressed(_gesture, _n_press, x, y):
        picked = content.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None:
            if isinstance(picked, Gtk.Editable):
                return
            picked = picked.get_parent()
        window.set_focus(None)

    click_away = Gtk.GestureClick.new()
    click_away.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    click_away.connect("pressed", on_content_pressed)
    content.add_controller(click_away)

    # -- save logic ------------------------------------------------------

    # The start / end selector (only for windows given a ``range_spec``).
    selector = None
    original_range = None
    if range_spec:
        selector = TimeRangeSelector(
            range_spec["duration"],
            range_spec.get("start"),
            range_spec.get("end"),
        )
        original_range = selector.stored_range()

    # The ReplayGain slider (only for windows given a ``replaygain_spec``).
    gain_slider = None
    gain_initial = None
    if replaygain_spec:
        gain_initial = replaygain_spec.get("initial")
        gain_slider = GainSlider("ReplayGain", gain_initial)
        form.append(gain_slider)

    def gain_changed():
        return gain_slider is not None and gain_slider.value != gain_initial

    # The "Compilation" checkbox (only for windows given a
    # ``compilation_spec``).
    compilation_check = None
    compilation_initial = False
    if compilation_spec:
        compilation_initial = bool(compilation_spec["initial"])
        compilation_check = Gtk.CheckButton(label="Compilation")
        compilation_check.set_active(compilation_initial)
        compilation_check.set_halign(Gtk.Align.START)
        compilation_check.set_tooltip_text(compilation_spec.get("tooltip"))
        # Under the cover, below the play count, sharing the room evenly.
        compilation_check.set_vexpand(True)
        compilation_check.set_valign(Gtk.Align.CENTER)
        cover_column.append(compilation_check)

    def compilation_changed():
        return (
            compilation_check is not None
            and compilation_check.get_active() != compilation_initial
        )

    def compilation_kind():
        """What to store: nothing special when the checkbox matches what the
        track's length says, otherwise the forced kind."""
        active = compilation_check.get_active()

        if active == compilation_initial:
            return compilation_spec.get("kind")

        if active == compilation_spec["automatic"]:
            return None

        return "compilation" if active else "track"

    def current_values():
        return {key: field.get_text() for key, field in fields.items()}

    def range_changed():
        return selector is not None and selector.stored_range() != original_range

    def can_save():
        if selector is not None and not selector.is_valid():
            return False

        values = current_values()

        if not all(check(values[key]) for key, check in validators.items()):
            return False

        return all(values[key] for key in required_keys) and (
            allow_unchanged
            or values != original
            or pending["data"] is not None
            or apply_state["on"]
            or range_changed()
            or gain_changed()
            or compilation_changed()
        )

    def on_changed(*_args):
        # A field that breaks its rule is outlined in red.
        for key, check in validators.items():
            if check(fields[key].get_text()):
                fields[key].entry.remove_css_class("error")
            else:
                fields[key].entry.add_css_class("error")

        save_button.set_sensitive(can_save())

    def on_save(*_args):
        if not can_save():
            return
        values = current_values()
        if pending["data"] is not None:
            values["image"] = pending["data"]
        if apply_state["on"]:
            values["apply_to_tracks"] = True
        if selector is not None:
            values["start_time"], values["end_time"] = selector.stored_range()
        if compilation_check is not None:
            values["kind"] = compilation_kind()
        if gain_slider is not None:
            values["replaygain"] = gain_slider.value
        window.close()
        callback(values)

    for field in fields.values():
        field.connect_changed(on_changed)
        field.connect_activate(on_save)

    if selector is not None:
        selector.connect_changed(on_changed)

    if compilation_check is not None:
        compilation_check.connect("toggled", on_changed)

    if gain_slider is not None:
        gain_slider.connect_changed(on_changed)

    def on_image_selected(data):
        # 256px, like the covers loaded from the library: a picture's natural
        # size is what the cover is laid out at, so a bigger texture would
        # blow the window up.
        texture = texture_from_bytes(data, 256)
        if texture is None:
            return
        pending["data"] = data
        cover.set_paintable(texture)
        cover_stack.set_visible_child_name("artwork")
        on_changed()

    def on_edit_image_clicked(_button):
        # Asked now, not when the window opened, so the search follows
        # whatever has been typed in the fields since.
        show_image_picker(
            window,
            image_search(current_values()),
            on_image_selected,
        )

    if edit_image_button is not None:
        edit_image_button.connect("clicked", on_edit_image_clicked)

    if apply_button is not None:
        def on_apply_toggled(button):
            apply_state["on"] = button.get_active()
            on_changed()

        apply_button.connect("toggled", on_apply_toggled)

    cancel_button.connect("clicked", lambda _button: window.close())
    save_button.connect("clicked", on_save)
    _close_on_escape(window)

    # Under both columns, at the very bottom of the window: the start / end
    # selector (the "Compilation" checkbox is with the fields, above).
    extras = []

    if selector is not None:
        extras.append(selector)

    body = content
    if extras:
        # The selector sits right under the columns, with only the usual gap
        # between the two.
        content.set_margin_bottom(0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.append(content)

        for extra in extras:
            extra.set_margin_start(GAP)
            extra.set_margin_end(GAP)
            body.append(extra)

        extras[-1].set_margin_bottom(GAP)

    toolbar.set_content(body)
    window.set_content(toolbar)
    window.present()
    fields[keys[0]].grab_focus(show_suggestions=False)


def show_edit_track_dialog(
        parent,
        track,
        callback,
        library_tracks=(),
        *,
        window_title="Edit track",
        hint=None,
        allow_unchanged=False,
        replaygain=False,
):
    """Edit a track's title (file name), artist and album.

    The artist and album fields list what already exists in
    ``library_tracks``. ``callback`` receives
    ``{"title": ..., "artist": ..., "album": ..., "start_time": ...,
    "end_time": ...}`` (the last two are ``None`` for the very start / end of
    the track).

    With ``replaygain`` the track's ReplayGain tag (in dB) is a slider too,
    and ``callback`` also receives it as ``"replaygain"`` (a number of dB, or
    None for no tag). A file type that cannot hold the tag shows a note
    instead.

    ``window_title`` and ``hint`` replace the window's title and the note
    under the fields, and ``allow_unchanged`` lets Save be pressed without
    changing anything: the import windows use all three.
    """
    library_tracks = list(library_tracks)
    track_path = track.get("path")

    # The album the field shows and that album's artist, to tell when the
    # track's artist is the artist of its album (see ``on_album_picked``).
    shown_album = {
        "title": str(track.get("album") or "").strip(),
        "artist": str(
            track.get("album_artist") or track.get("artist") or ""
        ).strip(),
    }

    def on_album_picked(suggestion, fields):
        """Picking another album in the list also gives the track that
        album's artist, when the track's artist was the artist of the album
        shown until now (so it follows its album, and a track on a
        compilation keeps its own artist)."""
        new_artist = str(suggestion.get("detail") or "").strip()
        follows = (
            shown_album["title"]
            and fields["album"].get_text().casefold()
            == shown_album["title"].casefold()
            and fields["artist"].get_text().casefold()
            == shown_album["artist"].casefold()
        )

        if follows and new_artist:
            fields["artist"].entry.set_text(new_artist)

        shown_album["title"] = str(suggestion["value"]).strip()
        shown_album["artist"] = new_artist

    def load_cover():
        # The track's own image only (embedded in its file). A track without
        # one shows no artwork here, never its album's.
        return get_cached_own_artwork(Path(track_path))

    specs = [
        {
            "key": "title",
            "label": "Title",
            "initial": track.get("title"),
        },
        {
            "key": "artist",
            "label": "Artist",
            "initial": track.get("artist"),
            "noun": "artist",
            "suggestions": _artist_suggestions(library_tracks),
        },
        {
            "key": "album",
            "label": "Album",
            "initial": track.get("album"),
            "noun": "album",
            "suggestions": _album_suggestions(library_tracks),
            # Albums by the artist typed above come first.
            "priority_field": "artist",
            # Left empty, the track is a single.
            "optional": True,
            "on_pick": on_album_picked,
        },
    ]

    # The track's ReplayGain tag, when ReplayGain is switched on.
    replaygain_spec = None
    replaygain_unsupported = False

    if replaygain and track_path:
        if supports_replaygain(track_path):
            # A track about to be imported may carry a gain measured just now;
            # otherwise it is the one the file is tagged with, if any.
            measured = track.get("replaygain")
            replaygain_spec = {
                "initial": (
                    measured if measured is not None
                    else read_track_gain(track_path)
                ),
            }
        else:
            replaygain_unsupported = True

    def image_search(values):
        # What is in the window, not what the track is right now: it may have
        # been changed above without being saved yet.
        return track_search(
            values["title"],
            values["artist"],
            values["album"],
            library_tracks,
        )

    # Where the track starts and stops playing (only when its length is
    # known).
    duration = float(track.get("duration") or 0)
    range_spec = (
        {
            "duration": duration,
            "start": track.get("start_time"),
            "end": track.get("end_time"),
        }
        if duration > 0 else None
    )

    # Single track or compilation (only when compilations are enabled).
    settings = get_settings()
    compilation_spec = None
    if settings.compilations_enabled:
        compilation_spec = {
            "initial": is_compilation(track),
            "automatic": automatic_is_compilation(track),
            "kind": track.get("kind"),
            "tooltip": (
                "A long audio file such as a mix or a live set. By "
                "default, audio longer than "
                f"{format_clock(settings.compilation_threshold)} is a "
                "compilation."
            ),
        }

    # How many times the track was played: shown for information only (a
    # track being imported has no count yet).
    info = None

    if track.get("play_count") is not None:
        info = [("Play count", str(int(track["play_count"])))]

    if replaygain_unsupported:
        info = list(info or ()) + [
            ("ReplayGain", "Not available for this type of file"),
        ]

    show_edit_dialog(
        parent,
        window_title,
        specs,
        callback,
        cover_loader=load_cover if track_path else None,
        hint=hint,
        image_search=image_search,
        range_spec=range_spec,
        compilation_spec=compilation_spec,
        allow_unchanged=allow_unchanged,
        info=info,
        replaygain_spec=replaygain_spec,
    )


def show_edit_album_dialog(parent, album, library_tracks, callback):
    """Edit an album's title and album artist.

    ``callback`` receives ``{"title": ..., "artist": ...}``.
    """
    library_tracks = list(library_tracks)

    specs = [
        {
            "key": "title",
            "label": "Album",
            "initial": album["title"],
            "noun": "album",
            "suggestions": _album_suggestions(library_tracks),
            "priority_field": "artist",
        },
        {
            "key": "artist",
            "label": "Album artist",
            "initial": album["artist"],
            "noun": "artist",
            "suggestions": _artist_suggestions(
                library_tracks, album_artists=True
            ),
        },
    ]

    show_edit_dialog(
        parent,
        "Edit album",
        specs,
        callback,
        cover_path=album.get("artwork_path"),
        placeholder_icon="media-optical-symbolic",
        hint=(
            "The album's files are renamed and moved to match its title "
            "and artist."
        ),
        image_search=lambda values: album_search(
            values["artist"],
            values["title"],
        ),
        apply_artwork=True,
    )


def show_edit_artist_dialog(parent, artist, library_tracks, callback):
    """Edit an artist's name. ``callback`` receives ``{"name": ...}``."""
    specs = [
        {
            "key": "name",
            "label": "Artist",
            "initial": artist["name"],
            "noun": "artist",
            "suggestions": _artist_suggestions(
                list(library_tracks), album_artists=True
            ),
        },
    ]

    show_edit_dialog(
        parent,
        "Edit artist",
        specs,
        callback,
        cover_path=artist.get("artwork_path"),
        placeholder_icon="avatar-default-symbolic",
        hint=(
            "All of this artist's files are renamed and moved to match. "
            "Picking an existing artist merges them into it."
        ),
        image_search=lambda values: artist_search(values["name"]),
        round_cover=True,
    )


def show_edit_playlist_dialog(
        parent,
        playlist,
        cover_path,
        callback,
        categories=(),
):
    """Edit a playlist's name, category and picture.

    ``categories`` are the names of the categories in use, offered as
    suggestions. ``callback`` receives ``{"name": ..., "category": ...}`` (the
    category is empty for none), plus ``"image"`` (raw bytes) when a new
    picture was chosen.
    """
    specs = [
        {
            "key": "name",
            "label": "Name",
            "initial": playlist["name"],
        },
        _category_spec(playlist.get("category"), categories),
    ]

    show_edit_dialog(
        parent,
        "Edit playlist",
        specs,
        callback,
        cover_path=cover_path,
        placeholder_icon="view-list-symbolic",
        hint=_CATEGORY_HINT,
        image_search=lambda values: playlist_search(),
    )


_CATEGORY_HINT = (
    "A playlist with a category is listed under Playlists in the sidebar, "
    "on a page of that name. Left empty, it stays in the Playlists page."
)


def _category_spec(initial, categories):
    """The field that gives a playlist its category (see
    ``show_edit_dialog``), listing the ``categories`` that exist."""
    return {
        "key": "category",
        "label": "Category",
        "initial": initial or "",
        "noun": "category",
        "suggestions": category_suggestions(categories),
        # Left empty, the playlist stays in the Playlists page.
        "optional": True,
        "placeholder": "None",
        "new_message": (
            "New category — it will be added under Playlists in the sidebar"
        ),
    }


def show_new_playlist_dialog(parent, categories, default_category, callback):
    """Ask for the name, and the category if any, of a new playlist.

    ``categories`` are the names of the categories in use, and the category
    field starts with ``default_category`` (the page the playlist is made
    from). ``callback`` receives ``{"name": ..., "category": ...}``, the
    category being empty for none.
    """
    specs = [
        {
            "key": "name",
            "label": "Name",
            "initial": "",
        },
        _category_spec(default_category, categories),
    ]

    _show_tracks_dialog(
        parent,
        "New playlist",
        _CATEGORY_HINT,
        specs,
        callback,
    )


def _show_tracks_dialog(parent, window_title, description, specs, callback):
    """A small window with a few fields, for a change made to several tracks
    at once.

    ``specs`` describes the fields as for ``show_edit_dialog`` (``key``,
    ``label``, ``initial``, ``noun``, ``suggestions``, ``priority_field``),
    plus an optional ``placeholder``, shown while a field is empty, and
    ``new_message`` (see ``show_edit_dialog``). Every field has to be filled
    in, except those marked ``optional``, and Save waits until one of them
    differs from where it started. ``callback`` receives ``{key: text}``.
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(window_title)
    window.set_resizable(False)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    save_button = Gtk.Button(label="Save")
    save_button.add_css_class("suggested-action")
    save_button.set_sensitive(False)
    header.pack_start(cancel_button)
    header.pack_end(save_button)
    toolbar.add_top_bar(header)

    original = {
        spec["key"]: str(spec.get("initial") or "").strip()
        for spec in specs
    }
    fields = {}

    for spec in specs:
        priority_key = spec.get("priority_field")
        priority = (
            (lambda key=priority_key: fields[key].get_text().casefold())
            if priority_key else None
        )
        field = SuggestionField(
            spec["label"],
            noun=spec.get("noun"),
            suggestions=spec.get("suggestions", ()),
            initial=original[spec["key"]],
            priority=priority,
            new_message=spec.get("new_message"),
        )

        if spec.get("placeholder"):
            field.entry.set_placeholder_text(spec["placeholder"])

        fields[spec["key"]] = field

    note = Gtk.Label(label=description, xalign=0, wrap=True)
    # Wrap to the column, never widen it.
    note.set_max_width_chars(1)
    note.add_css_class("dim-label")
    note.add_css_class("caption")

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    content.set_size_request(380, -1)
    content.set_margin_top(24)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    for field in fields.values():
        content.append(field)

    content.append(note)

    def current_values():
        return {key: field.get_text() for key, field in fields.items()}

    required = [spec["key"] for spec in specs if not spec.get("optional")]

    def can_save():
        values = current_values()
        return (
            all(values[key] for key in required)
            and values != original
        )

    def on_changed(*_args):
        save_button.set_sensitive(can_save())

    def on_save(*_args):
        if not can_save():
            return

        values = current_values()
        window.close()
        callback(values)

    for field in fields.values():
        field.connect_changed(on_changed)
        field.connect_activate(on_save)

    cancel_button.connect("clicked", lambda _button: window.close())
    save_button.connect("clicked", on_save)
    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()
    next(iter(fields.values())).grab_focus(show_suggestions=False)


def show_set_album_dialog(parent, tracks, library_tracks, callback):
    """Choose one album, and its album artist, for several tracks.

    The fields start with what the tracks have in common. Where they differ
    the field is empty, and Save waits for a value. ``callback`` receives
    ``{"album": ..., "artist": ...}``, the artist being the album artist.
    """
    library_tracks = list(library_tracks)
    albums = {str(track.get("album") or "").strip() for track in tracks}
    artists = {
        str(track.get("album_artist") or track.get("artist") or "").strip()
        for track in tracks
    }

    specs = [
        {
            "key": "album",
            "label": "Album",
            "initial": next(iter(albums)) if len(albums) == 1 else "",
            "placeholder": "Different albums" if len(albums) > 1 else None,
            "noun": "album",
            "suggestions": _album_suggestions(library_tracks),
            # Albums by the album artist typed below come first.
            "priority_field": "artist",
        },
        {
            "key": "artist",
            "label": "Album artist",
            "initial": next(iter(artists)) if len(artists) == 1 else "",
            "placeholder": "Different artists" if len(artists) > 1 else None,
            "noun": "artist",
            "suggestions": _artist_suggestions(
                library_tracks, album_artists=True
            ),
        },
    ]

    _show_tracks_dialog(
        parent,
        "Set album",
        (
            f"All {len(tracks)} tracks go into this album. Their files are "
            "renamed and moved to match its title and artist."
        ),
        specs,
        callback,
    )


def show_set_artist_dialog(parent, tracks, library_tracks, callback):
    """Choose one artist for several tracks.

    The field starts with the tracks' artist when it is the same for all of
    them. ``callback`` receives ``{"artist": ...}``.
    """
    artists = {str(track.get("artist") or "").strip() for track in tracks}

    specs = [
        {
            "key": "artist",
            "label": "Artist",
            "initial": next(iter(artists)) if len(artists) == 1 else "",
            "placeholder": "Different artists" if len(artists) > 1 else None,
            "noun": "artist",
            "suggestions": _artist_suggestions(list(library_tracks)),
        },
    ]

    _show_tracks_dialog(
        parent,
        "Set artist",
        (
            f"The artist of all {len(tracks)} tracks. It becomes their album "
            "artist too, so their files are moved to this artist's folder."
        ),
        specs,
        callback,
    )


def show_playlist_picker_dialog(
        parent,
        track,
        playlists,
        on_select,
        on_create=None,
):
    """Open a window listing playlists to add ``track`` to (it may also be a
    list of tracks).

    ``on_select(playlist_id)`` is called when a playlist is picked and
    ``on_create()`` when "New playlist…" is chosen (if provided).
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Add to playlist")
    window.set_default_size(420, -1)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    tracks = list(track) if isinstance(track, (list, tuple)) else [track]

    if len(tracks) == 1:
        track_title = str(tracks[0].get("title") or "Unknown Title")
        prompt_text = f"Choose a playlist for “{track_title}”"
    else:
        prompt_text = f"Choose a playlist for {len(tracks)} tracks"

    prompt = Gtk.Label(
        label=prompt_text,
        xalign=0,
        wrap=True,
    )
    prompt.add_css_class("dim-label")
    content.append(prompt)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.set_activate_on_single_click(True)
    listbox.add_css_class("boxed-list")

    def add_row(title, subtitle, icon_name, playlist_id):
        row = Adw.ActionRow()
        row.set_use_markup(False)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
        row.set_activatable(True)
        row.playlist_id = playlist_id
        listbox.append(row)

    if on_create is not None:
        add_row("New playlist…", None, "list-add-symbolic", None)

    for playlist in playlists:
        count = playlist.get("track_count", 0)
        add_row(
            playlist["name"],
            f"{count} track{'s' if count != 1 else ''}",
            "view-list-symbolic",
            playlist["id"],
        )

    if not playlists:
        empty = Gtk.Label(label="You don't have any playlists yet.", xalign=0)
        empty.add_css_class("dim-label")
        content.append(empty)

    def on_row_activated(_listbox, row):
        window.close()
        if row.playlist_id is None:
            on_create()
        else:
            on_select(row.playlist_id)

    listbox.connect("row-activated", on_row_activated)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_propagate_natural_height(True)
    scrolled.set_max_content_height(360)
    scrolled.set_child(listbox)
    content.append(scrolled)

    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()


# ----------------------------------------------------------------------
# Track context menu
# ----------------------------------------------------------------------

# Compact look shared by every right-click menu. The default popover padding,
# the box margins and the buttons' own padding all added up to a thick border
# and tall rows, so they are tightened here in one place.
MENU_PADDING = 6
MENU_WIDTH = 150  # total width of every menu, whatever its entries say
MENU_ITEM_PADDING_Y = 6
MENU_ITEM_PADDING_X = 6

_MENU_CSS = f"""
popover.context-menu > contents {{
    padding: {MENU_PADDING}px;
}}
popover.context-menu button.flat {{
    min-height: 0;
    padding: {MENU_ITEM_PADDING_Y}px {MENU_ITEM_PADDING_X}px;
}}
popover.context-menu separator {{
    margin: {MENU_PADDING}px 0;
}}
"""
_menu_css_installed = False


def _style_context_menu(popover):
    """Give ``popover`` the compact context-menu style (CSS installed once)."""
    global _menu_css_installed
    if not _menu_css_installed:
        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_string(_MENU_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            _menu_css_installed = True
    popover.add_css_class("context-menu")


# "Open album" / "Open artist" navigate the whole window, so the window
# registers them once here and every track menu (library, albums, artists,
# playlists) picks them up.
_track_navigation = {
    "open_album": None,
    "open_artist": None,
    "album_is_open": None,
    "artist_is_open": None,
}


def set_track_navigation(
        open_album,
        open_artist,
        album_is_open,
        artist_is_open,
):
    """Register what the track menus' "Open album" / "Open artist" do.

    ``open_album(track)`` / ``open_artist(track)`` navigate there;
    ``album_is_open(track)`` / ``artist_is_open(track)`` say whether that page
    is already showing (the entry is then left out).
    """
    _track_navigation.update(
        open_album=open_album,
        open_artist=open_artist,
        album_is_open=album_is_open,
        artist_is_open=artist_is_open,
    )


def open_artist_named(name):
    """Open the page of the artist called ``name`` (used by the album cards'
    "Open artist" entry). Does nothing until the window has registered the
    navigation with ``set_track_navigation``."""
    open_artist = _track_navigation["open_artist"]

    if open_artist is not None:
        open_artist({"artist": name})


def open_track_album(track):
    """Open the page of ``track``'s album (nothing for a single)."""
    open_album = _track_navigation["open_album"]

    if open_album is not None and has_album(track):
        open_album(track)


def open_track_artist(track):
    """Open the page of ``track``'s artist."""
    open_artist = _track_navigation["open_artist"]

    if open_artist is not None:
        open_artist(track)


def show_in_file_manager(parent, path):
    """Open the folder holding ``path`` in the file manager, with the file
    selected where the file manager supports it."""
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))

    def done(launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as error:
            # Closing the file manager's window without choosing is not an
            # error worth a dialog.
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                show_error_dialog(parent, "Could not open the folder", error.message)

    launcher.open_containing_folder(parent, None, done)


# "Play after" / "Add to queue" in a track's menu, registered once by the
# window like the navigation above.
_track_queue = {"play_after": None, "add_to_queue": None}


def set_track_queue_actions(play_after, add_to_queue):
    """Register what the track menus' "Play after" and "Add to queue" do.

    Both receive the track.
    """
    _track_queue.update(play_after=play_after, add_to_queue=add_to_queue)


# The right-click menu of several selected tracks. Like the entries above,
# what its entries do is registered once by the window. Every action
# receives the list of selected tracks, in the order the list shows them.
_tracks_actions = {}


def set_tracks_actions(**actions):
    """Register what the menu of several selected tracks does.

    ``play``, ``play_after``, ``add_to_queue``, ``set_album``, ``set_artist``,
    ``favorite`` and ``delete`` are called with the tracks. ``add_to_playlist``
    is called with them too, and with ``exclude_playlist_id=`` when the menu
    is a playlist's (the playlist the tracks are already in).
    """
    _tracks_actions.update(actions)


def run_tracks_action(name, tracks, **options):
    """Do one of the registered actions for ``tracks`` (nothing happens when
    there is none under ``name``)."""
    action = _tracks_actions.get(name)

    if action is not None and tracks:
        action(list(tracks), **options)


# "Delete" in a track's menu removes the track from the whole library, so,
# like the navigation above, the window registers it once for every menu.
_track_deletion = {"delete": None}


def set_track_deletion(delete_track):
    """Register what the track menus' "Delete" entry does.

    ``delete_track(track)`` asks for confirmation and deletes it.
    """
    _track_deletion["delete"] = delete_track


def _tracks_menu_entries(tracks, on_remove_from_playlist, exclude_playlist_id):
    """The entries of the menu of several selected tracks, as
    ``_show_entries_menu`` takes them."""
    entries = []

    def add(label, name, **options):
        if name in _tracks_actions:
            entries.append(
                (label, lambda: run_tracks_action(name, tracks, **options))
            )

    add("Play", "play")
    add("Play after", "play_after")
    add("Add to queue", "add_to_queue")

    entries.append(None)
    add("Set album…", "set_album")
    add("Set artist…", "set_artist")

    entries.append(None)
    add(
        "Add to playlist…",
        "add_to_playlist",
        exclude_playlist_id=exclude_playlist_id,
    )
    add(
        "Remove from favorites"
        if all(track.get("favorite") for track in tracks)
        else "Add to favorites",
        "favorite",
    )

    if on_remove_from_playlist is not None:
        entries.append(
            (
                "Remove from playlist",
                lambda: on_remove_from_playlist(tracks),
            )
        )

    if "delete" in _tracks_actions:
        entries.append(None)
        add("Delete", "delete")

    return entries


def attach_track_context_menu(
        row,
        track,
        *,
        on_play_pause,
        is_playing,
        on_edit,
        on_add_to_playlist,
        on_remove_from_playlist=None,
        on_remove_many_from_playlist=None,
        exclude_playlist_id=None,
        on_view=None,
        position=Gtk.PositionType.BOTTOM,
):
    """Right-click menu for a track row.

    ``track`` may also be a function returning the track (or None, for no
    menu), for a widget whose track changes, like the player bar's.
    With ``on_view``, a "View" entry (next to "Open album" and "Open artist")
    calls it with the track.

    Every action is a flat menu entry; anything needing more input (editing,
    choosing a playlist) opens its own window instead of a sub-menu.
    All callbacks receive the track. ``is_playing(track)`` decides whether
    the first entry reads "Play" or "Pause".

    In a list that selects several tracks, a right click on one of several
    selected tracks opens the menu of the whole selection instead (see
    ``set_tracks_actions``). ``on_remove_many_from_playlist(tracks)`` is that
    menu's "Remove from playlist", and ``exclude_playlist_id`` the playlist
    its "Add to playlist…" leaves out.
    """
    gesture = Gtk.GestureClick.new()
    gesture.set_button(3)

    def current_track():
        return track() if callable(track) else track

    def on_pressed(gesture, press_count, x, y):
        track = current_track()

        if track is None:
            return

        # The list would also take this click for a plain click on the row
        # when it is released, and drop a selection of several tracks.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        # Right-clicking a track selects it at once. A track that is one of
        # several selected keeps the selection, and the menu is then the one
        # of the whole selection.
        parent = row.get_parent()
        selected = []

        if isinstance(parent, Gtk.ListBox):
            multiple = (
                parent.get_selection_mode() == Gtk.SelectionMode.MULTIPLE
            )

            if multiple and row.is_selected():
                selected = selected_tracks(parent)

            if len(selected) < 2:
                selected = []
                alone = (
                    row.is_selected()
                    and len(parent.get_selected_rows()) == 1
                )

                if not alone:
                    if multiple:
                        parent.unselect_all()

                    parent.select_row(row)

        if len(selected) > 1:
            _show_entries_menu(
                row,
                x,
                y,
                _tracks_menu_entries(
                    selected,
                    on_remove_many_from_playlist,
                    exclude_playlist_id,
                ),
                position=position,
                heading=f"{len(selected)} tracks selected",
            )
            return

        popover = Gtk.Popover()
        popover.set_parent(row)
        _style_context_menu(popover)
        # Like a native context menu: no arrow, with the menu's top-left
        # corner at the pointer.
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        popover.set_position(position)

        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        def add_item(label, callback):
            button = Gtk.Button()
            button.set_has_frame(False)
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            button.set_child(Gtk.Label(label=label, xalign=0))

            def activate(_button):
                popover.popdown()
                callback(track)

            button.connect("clicked", activate)
            menu.append(button)

        add_item("Pause" if is_playing(track) else "Play", on_play_pause)

        if _track_queue["play_after"] is not None:
            add_item("Play after", _track_queue["play_after"])

        if _track_queue["add_to_queue"] is not None:
            add_item("Add to queue", _track_queue["add_to_queue"])

        menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        navigation = _track_navigation
        if (
            navigation["open_album"] is not None
            and has_album(track)
            and not navigation["album_is_open"](track)
        ):
            add_item("Open album", navigation["open_album"])
        if navigation["open_artist"] is not None and not navigation["artist_is_open"](track):
            add_item("Open artist", navigation["open_artist"])

        if on_view is not None:
            add_item("View", on_view)

        for label, action in artwork_menu_entries("track"):
            add_item(label, action)

        add_item("Edit…", on_edit)

        menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        add_item("Add to playlist…", on_add_to_playlist)
        add_item(
            "Remove from favorites"
            if track.get("favorite")
            else "Add to favorites",
            toggle_favorite,
        )

        if on_remove_from_playlist is not None:
            add_item("Remove from playlist", on_remove_from_playlist)

        if _track_deletion["delete"] is not None:
            menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            add_item("Delete", _track_deletion["delete"])

        # (x, y) are relative to ``row``, which is what the popover is
        # parented to, so point at them directly.
        menu.set_size_request(MENU_WIDTH - 2 * MENU_PADDING, -1)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        popover.set_child(menu)
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda _popover: popover.unparent())
        popover.popup()

    gesture.connect("pressed", on_pressed)
    row.add_controller(gesture)


def attach_context_menu(widget, get_entries):
    """Right-click menu for any widget (used by the grid cards).

    ``get_entries()`` is called when the menu opens and returns a list of
    ``(label, callback)`` pairs, so labels like Play/Pause are always current.
    A ``None`` in the list is drawn as a separator.
    """
    gesture = Gtk.GestureClick.new()
    gesture.set_button(3)

    def on_pressed(_gesture, _press_count, x, y):
        _show_entries_menu(widget, x, y, get_entries())

    gesture.connect("pressed", on_pressed)
    widget.add_controller(gesture)


def _show_entries_menu(
        widget,
        x,
        y,
        entries,
        *,
        position=Gtk.PositionType.BOTTOM,
        heading=None,
):
    """Open a context menu on ``widget``, its top-left corner at (x, y).

    ``entries`` is a list of ``(label, callback)`` pairs, ``callback()`` being
    called with nothing; a ``None`` in it is drawn as a separator. ``heading``
    is an optional line of dimmed text above the entries.
    """
    popover = Gtk.Popover()
    popover.set_parent(widget)
    _style_context_menu(popover)
    popover.set_has_arrow(False)
    popover.set_halign(Gtk.Align.START)
    popover.set_position(position)

    menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    if heading:
        title = Gtk.Label(label=heading, xalign=0)
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        title.set_margin_start(MENU_ITEM_PADDING_X)
        title.set_margin_end(MENU_ITEM_PADDING_X)
        title.set_margin_top(MENU_ITEM_PADDING_Y)
        title.set_margin_bottom(MENU_ITEM_PADDING_Y)
        menu.append(title)
        menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    def add_item(label, callback):
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("flat")
        button.set_halign(Gtk.Align.FILL)
        button.set_child(Gtk.Label(label=label, xalign=0))

        def activate(_button):
            popover.popdown()
            callback()

        button.connect("clicked", activate)
        menu.append(button)

    for entry in entries:
        if entry is None:
            menu.append(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            )
            continue

        label, callback = entry
        add_item(label, callback)

    menu.set_size_request(MENU_WIDTH - 2 * MENU_PADDING, -1)

    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = 1
    rect.height = 1

    popover.set_child(menu)
    popover.set_pointing_to(rect)
    popover.connect("closed", lambda _popover: popover.unparent())
    popover.popup()
