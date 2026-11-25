"""The windows used to import music into the library.

* ``show_import_dialog``: the popup that asks *how* to import: one row for
  each import source (see ``melody.import_sources``).
* ``show_link_dialog``: asks for a link and shows the download while it runs.
* ``show_search_dialog``: searches and lists the results, with their
  thumbnails, for the person to pick one.
* ``pick_audio_files``: the file chooser behind "From file".
* ``show_import_preview``: a small library of the files about to be imported,
  with an edit button on each, and Save / Cancel for the lot.

Nothing here touches the library: the caller decides what Save does.
"""

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from melody.services.artwork import get_cached_own_artwork
from melody.services.collection_actions import (
    _close_on_escape,
    show_edit_track_dialog,
)
from melody.services.compilations import format_clock
from melody.services.library_scanner import SUPPORTED_EXTENSIONS
from melody.services.track_range import track_length
from melody.widgets.image_picker import texture_from_bytes


THUMBNAIL_SIZE = 32


def create_import_button(on_clicked):
    """The "Import" button shown on the pages that can import music.

    ``on_clicked()`` is called without arguments.
    """
    button = Gtk.Button()
    button.set_child(
        Adw.ButtonContent(icon_name="list-add-symbolic", label="Import")
    )
    button.set_tooltip_text("Import music")
    button.connect("clicked", lambda _button: on_clicked())
    return button


# ----------------------------------------------------------------------
# How to import
# ----------------------------------------------------------------------

def show_import_dialog(parent, sources, on_pick):
    """Ask how to import music: one row for each of ``sources`` (see
    ``melody.import_sources``). ``on_pick(source)`` is called with the one the
    person picks, once this window is closed."""
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Import music")
    # Fixed size: the width given here, the height the content needs.
    window.set_resizable(False)
    window.set_size_request(420, -1)
    window.set_default_size(420, -1)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    prompt = Gtk.Label(label="How do you want to import music?", xalign=0)
    prompt.add_css_class("dim-label")
    content.append(prompt)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.set_activate_on_single_click(True)
    listbox.add_css_class("boxed-list")

    rows = {}

    for source in sources:
        row = Adw.ActionRow()
        row.set_use_markup(False)
        row.set_title(source.title)
        row.set_subtitle(source.subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(source.icon_name))
        row.set_activatable(True)
        listbox.append(row)
        rows[row] = source

    def on_row_activated(_listbox, row):
        window.close()
        on_pick(rows[row])

    listbox.connect("row-activated", on_row_activated)
    content.append(listbox)

    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()


# ----------------------------------------------------------------------
# A link
# ----------------------------------------------------------------------

def show_link_dialog(
        parent,
        source,
        on_done,
        discard=None,
        initial_url=None,
):
    """Ask for a link, and fetch it in the background with ``source`` (a
    ``LinkSource``, see ``melody.import_sources``).

    ``source.fetch(url, on_progress, cancel)`` blocks (it runs in a thread) and
    returns its result. If it fails it raises an exception whose message is
    shown in the window, which stays open so the person can try again.
    ``on_progress(fraction, text)`` may be called from that thread
    (``fraction`` is None while unknown) and ``cancel`` is a
    ``threading.Event`` set when the person cancels.

    On success the window closes and ``on_done(result)`` is called. If the
    window was closed before the result arrived, ``discard(result)`` gets it
    instead (to delete what was downloaded).

    With ``initial_url`` the link is filled in and the download starts at
    once (a result picked in the search window).
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(source.link_title)
    # Fixed size: the width given here, the height the content needs (it
    # grows a little when the progress and its messages appear).
    window.set_resizable(False)
    window.set_size_request(460, -1)
    window.set_default_size(460, -1)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    download_button = Gtk.Button(label="Download")
    download_button.add_css_class("suggested-action")
    download_button.set_sensitive(False)
    header.pack_start(cancel_button)
    header.pack_end(download_button)
    toolbar.add_top_bar(header)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    prompt = Gtk.Label(
        label=source.prompt,
        xalign=0,
    )
    prompt.add_css_class("dim-label")
    content.append(prompt)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")

    entry = Adw.EntryRow()
    entry.set_title(source.link_label)
    listbox.append(entry)
    content.append(listbox)

    progress = Gtk.ProgressBar()
    progress.set_visible(False)
    content.append(progress)

    status = Gtk.Label(xalign=0, wrap=True)
    status.set_visible(False)
    content.append(status)

    if source.notice:
        notice = Gtk.Label(label=source.notice, xalign=0, wrap=True)
        notice.add_css_class("dim-label")
        notice.add_css_class("caption")
        content.append(notice)

    state = {"busy": False, "closed": False, "done": False}
    cancel = threading.Event()

    def update_buttons():
        if state["busy"]:
            # A field that has the keyboard focus must not be disabled: GTK
            # would be left with a text field that never learns it lost the
            # focus ("GtkText - did not receive a focus-out event").
            window.set_focus(None)

        download_button.set_sensitive(
            not state["busy"] and source.accepts(entry.get_text())
        )
        entry.set_sensitive(not state["busy"])

    def show_status(text, error=False):
        status.set_label(text)
        status.set_visible(bool(text))

        if error:
            status.remove_css_class("dim-label")
            status.add_css_class("error")
        else:
            status.remove_css_class("error")
            status.add_css_class("dim-label")

    def show_progress(fraction, text):
        if not state["closed"]:
            show_status(text)

            if fraction is None:
                progress.pulse()
            else:
                progress.set_fraction(fraction)

        return GLib.SOURCE_REMOVE

    def on_progress(fraction, text):
        # Called from the download thread.
        GLib.idle_add(show_progress, fraction, text)

    def finished(result, error):
        state["busy"] = False

        if state["closed"]:
            # The window is gone: nobody is waiting for this result.
            if result is not None and discard is not None:
                discard(result)

            return GLib.SOURCE_REMOVE

        if error is not None:
            progress.set_visible(False)
            show_status(error, error=True)
            update_buttons()
            # Ready for another try.
            entry.grab_focus()
            return GLib.SOURCE_REMOVE

        state["done"] = True
        window.close()
        on_done(result)
        return GLib.SOURCE_REMOVE

    def worker(url):
        try:
            result = source.fetch(url, on_progress, cancel)
        except Exception as error:
            GLib.idle_add(finished, None, str(error) or "Download failed.")
        else:
            GLib.idle_add(finished, result, None)

    def start(*_args):
        url = entry.get_text().strip()

        if state["busy"] or not source.accepts(url):
            return

        state["busy"] = True
        update_buttons()
        progress.set_fraction(0)
        progress.set_visible(True)
        progress.pulse()
        show_status("Looking the video up…")

        threading.Thread(target=worker, args=(url,), daemon=True).start()

    def on_close_request(_window):
        state["closed"] = True

        if not state["done"]:
            cancel.set()

        return False

    entry.connect("changed", lambda _entry: update_buttons())
    entry.connect("entry-activated", start)
    download_button.connect("clicked", start)
    cancel_button.connect("clicked", lambda _button: window.close())
    window.connect("close-request", on_close_request)
    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()

    if initial_url:
        # The download starts at once: nothing to type, and a field that has
        # the focus cannot be disabled (see ``update_buttons``).
        entry.set_text(initial_url)

        def start_now():
            start()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(start_now)
    else:
        entry.grab_focus()


# ----------------------------------------------------------------------
# Searching
# ----------------------------------------------------------------------

SEARCH_THUMBNAIL_WIDTH = 96
SEARCH_THUMBNAIL_HEIGHT = 54


def _clock(seconds):
    if not seconds:
        return ""

    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"


class _SearchRow(Gtk.ListBoxRow):
    """One search result: thumbnail, title, artist and length."""

    def __init__(self, result):
        super().__init__()

        self.result = result

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(12)

        # Empty (just a dark card) until the picture has been fetched.
        self.thumbnail = Gtk.Picture()
        self.thumbnail.set_size_request(
            SEARCH_THUMBNAIL_WIDTH,
            SEARCH_THUMBNAIL_HEIGHT,
        )
        self.thumbnail.set_content_fit(Gtk.ContentFit.COVER)
        self.thumbnail.set_can_shrink(True)
        self.thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        self.thumbnail.set_valign(Gtk.Align.CENTER)
        self.thumbnail.add_css_class("card")
        box.append(self.thumbnail)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label=result.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        texts.append(title)

        artist = Gtk.Label(label=result.artist or "Unknown Artist", xalign=0)
        artist.set_ellipsize(Pango.EllipsizeMode.END)
        artist.add_css_class("dim-label")
        artist.add_css_class("caption")
        texts.append(artist)

        box.append(texts)

        duration = Gtk.Label(label=_clock(result.duration), xalign=1)
        duration.set_size_request(52, -1)
        duration.add_css_class("dim-label")
        duration.add_css_class("numeric")
        box.append(duration)

        self.set_child(box)

    def show_thumbnail(self, data):
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error:
            return

        self.thumbnail.set_paintable(texture)


def show_search_dialog(parent, source, on_pick):
    """Search with ``source`` (a ``SearchSource``, see
    ``melody.import_sources``) and let the person pick one of the results.

    ``source.search(query)`` blocks (it runs in a thread) and returns a list
    of ``SearchResult``; if it fails it raises an exception whose message is
    shown in the window. ``source.load_thumbnail(url)`` blocks too and
    returns the picture's bytes (a picture that fails to load is simply left
    empty).

    Picking a result (the Download button, or a double click) closes the
    window and calls ``on_pick(result)``; Cancel picks nothing.
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(source.search_title)
    # Fixed size: the results scroll inside it.
    window.set_resizable(False)
    window.set_size_request(560, 640)
    window.set_default_size(560, 640)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    pick_button = Gtk.Button(label="Download")
    pick_button.add_css_class("suggested-action")
    pick_button.set_sensitive(False)
    header.pack_start(cancel_button)
    header.pack_end(pick_button)
    toolbar.add_top_bar(header)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    entry = Gtk.SearchEntry()
    entry.set_placeholder_text(source.search_placeholder)
    content.append(entry)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
    listbox.set_activate_on_single_click(False)
    listbox.add_css_class("boxed-list")

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    scrolled.set_child(listbox)

    message = Adw.StatusPage()
    message.set_vexpand(True)

    stack = Gtk.Stack()
    stack.set_vexpand(True)
    stack.add_named(message, "message")
    stack.add_named(scrolled, "results")
    content.append(stack)

    # ``search_id`` tells the searches apart: only the latest one is shown.
    state = {"closed": False, "search_id": 0}

    def show_message(icon, title, description=""):
        message.set_icon_name(icon)
        message.set_title(title)
        message.set_description(description)
        stack.set_visible_child_name("message")
        pick_button.set_sensitive(False)

    def current(search_id):
        return not state["closed"] and search_id == state["search_id"]

    def clear_results():
        child = listbox.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def show_thumbnail(search_id, row, data):
        if current(search_id):
            row.show_thumbnail(data)

        return GLib.SOURCE_REMOVE

    def load_thumbnails(search_id, rows):
        # One after the other, in the background: the list shows at once.
        for row in rows:
            if not current(search_id):
                return

            if not row.result.thumbnail_url:
                continue

            try:
                data = source.load_thumbnail(row.result.thumbnail_url)
            except Exception:
                continue

            GLib.idle_add(show_thumbnail, search_id, row, data)

    def show_results(search_id, results):
        if not current(search_id):
            return GLib.SOURCE_REMOVE

        clear_results()

        if not results:
            show_message(
                "system-search-symbolic",
                "No results",
                "Try different words.",
            )
            return GLib.SOURCE_REMOVE

        rows = []

        for result in results:
            row = _SearchRow(result)
            rows.append(row)
            listbox.append(row)

        pick_button.set_sensitive(False)
        stack.set_visible_child_name("results")

        threading.Thread(
            target=load_thumbnails,
            args=(search_id, rows),
            daemon=True,
        ).start()

        return GLib.SOURCE_REMOVE

    def search_failed(search_id, text):
        if current(search_id):
            show_message("dialog-error-symbolic", "Search failed", text)

        return GLib.SOURCE_REMOVE

    def worker(search_id, query):
        try:
            results = source.search(query)
        except Exception as error:
            GLib.idle_add(
                search_failed,
                search_id,
                str(error) or "The search failed.",
            )
        else:
            GLib.idle_add(show_results, search_id, results)

    def start_search(*_args):
        query = entry.get_text().strip()

        if not query:
            return

        state["search_id"] += 1
        show_message("system-search-symbolic", "Searching…")

        threading.Thread(
            target=worker,
            args=(state["search_id"], query),
            daemon=True,
        ).start()

    def pick(row=None):
        row = row or listbox.get_selected_row()

        if row is None:
            return

        # Closing a window with a text field that still has the focus makes
        # GTK warn that it never lost it.
        window.set_focus(None)
        window.close()
        on_pick(row.result)

    def on_close_request(_window):
        state["closed"] = True
        return False

    show_message(
        "system-search-symbolic",
        source.search_title,
        "Type a song or an artist and press Enter.",
    )

    entry.connect("activate", start_search)
    listbox.connect(
        "row-selected",
        lambda _listbox, row: pick_button.set_sensitive(row is not None),
    )
    listbox.connect("row-activated", lambda _listbox, row: pick(row))
    pick_button.connect("clicked", lambda _button: pick())
    cancel_button.connect("clicked", lambda _button: window.close())
    window.connect("close-request", on_close_request)
    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()
    entry.grab_focus()


# ----------------------------------------------------------------------
# Choosing files
# ----------------------------------------------------------------------

def pick_audio_files(parent, callback):
    """Open a file chooser for audio files.

    ``callback(paths)`` receives a list of ``Path`` (one or several). It is
    not called if the chooser is dismissed.
    """
    audio_filter = Gtk.FileFilter()
    audio_filter.set_name("Audio files")
    audio_filter.add_mime_type("audio/*")

    for extension in sorted(SUPPORTED_EXTENSIONS):
        audio_filter.add_suffix(extension.lstrip("."))

    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(audio_filter)

    def on_chosen(dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # dismissed

        paths = []

        for index in range(files.get_n_items()):
            path = files.get_item(index).get_path()

            if path:
                paths.append(Path(path))

        if paths:
            callback(paths)

    dialog = Gtk.FileDialog()
    dialog.set_title("Select music files")
    dialog.set_filters(filters)
    dialog.set_default_filter(audio_filter)
    dialog.open_multiple(parent, None, on_chosen)


# ----------------------------------------------------------------------
# Several files: a preview of what will be imported
# ----------------------------------------------------------------------

class _PendingRow(Gtk.ListBoxRow):
    """One file about to be imported, laid out like a row of the library,
    with an edit button at the end."""

    def __init__(self, pending, on_edit):
        super().__init__()

        self.pending = pending
        self.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(12)

        self.thumbnail = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic"
        )
        # The thumbnail keeps its size; only the music note inside it is
        # smaller. A real cover is drawn at the full size.
        self.thumbnail.set_pixel_size(round(THUMBNAIL_SIZE * 0.7))
        self.thumbnail.set_size_request(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        self.thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        self.thumbnail.add_css_class("card")
        box.append(self.thumbnail)

        self.title_label = Gtk.Label(xalign=0)
        self.title_label.set_hexpand(True)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.add_css_class("heading")
        box.append(self.title_label)

        self.album_label = Gtk.Label(xalign=0)
        self.album_label.set_size_request(130, -1)
        self.album_label.set_max_width_chars(1)
        self.album_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.album_label.add_css_class("dim-label")
        box.append(self.album_label)

        self.artist_label = Gtk.Label(xalign=0)
        self.artist_label.set_size_request(120, -1)
        self.artist_label.set_max_width_chars(1)
        self.artist_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist_label.add_css_class("dim-label")
        box.append(self.artist_label)

        self.duration_label = Gtk.Label(xalign=1)
        self.duration_label.set_size_request(52, -1)
        self.duration_label.add_css_class("dim-label")
        self.duration_label.add_css_class("numeric")
        box.append(self.duration_label)

        edit_button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        edit_button.add_css_class("flat")
        edit_button.add_css_class("circular")
        edit_button.set_valign(Gtk.Align.CENTER)
        edit_button.set_tooltip_text("Edit")
        edit_button.connect("clicked", lambda _button: on_edit(self))
        box.append(edit_button)

        self.set_child(box)
        self.update()

    def update(self):
        """Show the pending track's current values."""
        track = self.pending.as_track()

        self.title_label.set_label(track["title"] or "Unknown Title")
        self.album_label.set_label(track["album"])
        self.artist_label.set_label(track["artist"] or "Unknown Artist")
        self.duration_label.set_label(format_clock(track_length(track)))

        # A cover picked in the edit window replaces the file's own.
        if self.pending.image:
            texture = texture_from_bytes(self.pending.image, THUMBNAIL_SIZE * 2)

            if texture is not None:
                self.thumbnail.set_from_paintable(texture)
                self.thumbnail.set_pixel_size(THUMBNAIL_SIZE)

    def show_file_cover(self, path):
        """The cover found in the file (never over one picked by hand)."""
        if self.pending.image or not path or not Path(path).is_file():
            return

        self.thumbnail.set_from_file(str(path))
        self.thumbnail.set_pixel_size(THUMBNAIL_SIZE)


def show_import_preview(
        parent,
        pending,
        library_tracks,
        on_save,
        replaygain=False,
):
    """Show the files about to be imported, each with an edit button.

    ``pending`` is a list of ``PendingTrack``; editing one changes it in
    place. Save closes the window and calls ``on_save(pending)``; Cancel (or
    closing the window) imports nothing. With ``replaygain`` the edit window
    of each track has its ReplayGain slider.
    """
    library_tracks = list(library_tracks)

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(f"Import {len(pending)} tracks")
    # Fixed size: the list of files scrolls inside it.
    window.set_resizable(False)
    window.set_size_request(760, 520)
    window.set_default_size(760, 520)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    save_button = Gtk.Button(label="Save")
    save_button.add_css_class("suggested-action")
    header.pack_start(cancel_button)
    header.pack_end(save_button)
    toolbar.add_top_bar(header)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")

    state = {"closed": False}
    rows = []

    def on_edit(row):
        item = row.pending

        def on_edited(values):
            item.apply(values)
            row.update()

        # The other files in the list count too: a new album or artist typed
        # for one of them is offered for the next.
        others = [
            other.as_track() for other in pending if other is not item
        ]

        show_edit_track_dialog(
            window,
            item.as_track(),
            on_edited,
            library_tracks + others,
            hint=(
                "This only changes what will be imported: the file is "
                "copied into your library, named after its title, artist "
                "and album, when you save."
            ),
            replaygain=replaygain,
        )

    for item in pending:
        row = _PendingRow(item, on_edit)
        rows.append(row)
        listbox.append(row)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    note = Gtk.Label(
        label=(
            "These files will be copied into your library. Use the edit "
            "button to change a track before saving."
        ),
        xalign=0,
        wrap=True,
    )
    note.add_css_class("dim-label")
    content.append(note)

    scrolled.set_child(listbox)
    content.append(scrolled)

    # The covers are read from the files one after the other in the
    # background, so the window opens at once.
    def show_cover(row, path):
        if not state["closed"]:
            row.show_file_cover(path)

        return GLib.SOURCE_REMOVE

    def load_covers():
        for row in rows:
            if state["closed"]:
                return

            try:
                path = get_cached_own_artwork(row.pending.path)
            except Exception:
                path = None

            if path:
                GLib.idle_add(show_cover, row, path)

    threading.Thread(target=load_covers, daemon=True).start()

    def on_close_request(_window):
        state["closed"] = True
        return False

    window.connect("close-request", on_close_request)

    def on_save_clicked(_button):
        window.close()
        on_save(pending)

    cancel_button.connect("clicked", lambda _button: window.close())
    save_button.connect("clicked", on_save_clicked)
    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()
