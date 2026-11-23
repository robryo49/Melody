"""The popup that lets an edit window pick a new image.

At the top, a "Select image…" button (a file chooser) and, for a track, a
"Use album cover" button. Below them, the suggestions from other sites
(Deezer, iTunes...) as a row of tiles, then a search bar and the thumbnails of
what YouTube finds for it. What is searched for, and how, is described by an
``ImageSearch`` (see ``services.image_sources``). Results are fetched in
background threads, so the window opens instantly; a result whose picture
turns out to be unusable simply disappears.

``show_image_picker`` only *picks*: the chosen image's bytes are handed to
``on_select`` and it is up to the caller to apply them (the edit windows
stage the image and write it when the person presses Save).
"""

import threading
from io import BytesIO
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango
from PIL import Image, ImageOps

TILE_SIZE = 112
TILE_SPACING = 12
# The window is exactly this many tiles wide (it cannot be resized), and its
# list of YouTube results is at most this many rows tall before it scrolls.
TILES_PER_LINE = 3
MAX_RESULT_ROWS = 3
MAX_FILE_SIZE = 20 * 1024 * 1024
# How many thumbnails are fetched at once.
MAX_CONCURRENT_LOADS = 6
# Two results whose fingerprints differ by at most this many of 64 bits show
# the same picture.
SAME_PICTURE_BITS = 4


def thumbnail_png(data, size):
    """Square, centre-cropped PNG of ``data`` (None if it isn't an image).

    Pure PIL work, so it is safe to call from a background thread.
    """
    try:
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image = ImageOps.fit(
                image,
                (size, size),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    except Exception:
        return None


def texture_from_png(png):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
    except GLib.Error:
        return None


def texture_from_bytes(data, size):
    """A square ``Gdk.Texture`` of an image (None if it isn't one)."""
    png = thumbnail_png(data, size)
    return texture_from_png(png) if png else None


def _is_image(data):
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:
        return False


def _fingerprint(png):
    """64 bits that two copies of one picture share, even when they were
    encoded differently (the average hash: each bit says whether a pixel of
    an 8x8 greyscale version is brighter than the average)."""
    with Image.open(BytesIO(png)) as source:
        small = source.convert("L").resize(
            (8, 8), Image.Resampling.BILINEAR,
        )

    pixels = small.tobytes()
    average = sum(pixels) / len(pixels)

    return sum(
        1 << index
        for index, value in enumerate(pixels)
        if value > average
    )


def _tile_cell_size():
    """(width, height) of one cell of a thumbnail grid.

    A tile is ``TILE_SIZE`` square, but the button around it and the flow
    box's own wrapper add padding that depends on the theme, so one cell is
    measured instead of guessed. Used to size the window to exactly
    ``TILES_PER_LINE`` tiles across and ``MAX_RESULT_ROWS`` rows down.
    """
    try:
        probe = Gtk.FlowBox()

        button = Gtk.Button()
        button.add_css_class("flat")

        inner = Gtk.Box()
        inner.set_size_request(TILE_SIZE, TILE_SIZE)
        button.set_child(inner)
        probe.append(button)

        width = probe.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        height = probe.measure(Gtk.Orientation.VERTICAL, width)[0]
    except Exception:
        width = height = 0

    # Never less than the tile itself; a failed measurement falls back on
    # the theme's usual padding (10px each side of the button, 3px for the
    # flow box child).
    if width <= TILE_SIZE or height <= TILE_SIZE:
        return TILE_SIZE + 26, TILE_SIZE + 22

    return width, height


class _Tile(Gtk.Button):
    """One result: its thumbnail, with its title as a tooltip."""

    def __init__(self, result):
        super().__init__()

        self.result = result

        self.add_css_class("flat")
        self.set_tooltip_text(
            f"{result.title}\n{result.detail}"
            if result.detail
            else result.title
        )

        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        spinner.start()

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_can_shrink(True)

        # Exactly the tile's size: a picture's natural size would otherwise
        # stretch the tile.
        self._stack = Gtk.Stack()
        self._stack.add_css_class("card")
        self._stack.set_overflow(Gtk.Overflow.HIDDEN)
        self._stack.set_size_request(TILE_SIZE, TILE_SIZE)
        self._stack.add_named(spinner, "loading")
        self._stack.add_named(self._picture, "image")
        self._stack.set_visible_child_name("loading")

        self.set_child(self._stack)
        self.set_sensitive(False)

        self._has_picture = False

    def show_thumbnail(self, texture):
        self._picture.set_paintable(texture)
        self._has_picture = True
        self._stack.set_visible_child_name("image")
        self.set_sensitive(True)

    def set_busy(self, busy):
        """A spinner over the tile while the full-size image is fetched."""
        if busy:
            self._stack.set_visible_child_name("loading")
        elif self._has_picture:
            self._stack.set_visible_child_name("image")

    def remove_from_results(self):
        # The tile is wrapped in a Gtk.FlowBoxChild, which is what takes up
        # room in the grid.
        wrapper = self.get_parent()

        if wrapper is not None:
            wrapper.set_visible(False)


class _SourceTile(Gtk.Button):
    """One suggestion from another site (Deezer, iTunes...): its picture and
    the site's name. Without a picture it shows a placeholder."""

    def __init__(self, source):
        super().__init__()

        self.source = source
        self.data = None

        self.add_css_class("flat")
        self.set_sensitive(False)

        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        spinner.start()

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_can_shrink(True)

        empty = Gtk.Image.new_from_icon_name("image-missing-symbolic")
        empty.set_pixel_size(32)
        empty.add_css_class("dim-label")

        self._stack = Gtk.Stack()
        self._stack.add_css_class("card")
        self._stack.set_overflow(Gtk.Overflow.HIDDEN)
        self._stack.set_size_request(TILE_SIZE, TILE_SIZE)
        self._stack.add_named(spinner, "loading")
        self._stack.add_named(self._picture, "image")
        self._stack.add_named(empty, "empty")
        self._stack.set_visible_child_name("loading")

        label = Gtk.Label(label=source.name)
        label.add_css_class("caption")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(14)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._stack)
        box.append(label)
        self.set_child(box)

    def show_image(self, data, texture):
        self.data = data
        self._picture.set_paintable(texture)
        self._stack.set_visible_child_name("image")
        self.set_tooltip_text(self.source.detail or None)
        self.set_sensitive(True)

    def show_empty(self):
        self._stack.set_visible_child_name("empty")
        self.set_tooltip_text(f"{self.source.name} has no image for this")


def show_image_picker(parent, image_search, on_select, *, title="Change image"):
    """Open the picker.

    ``image_search`` is an ``ImageSearch`` (see ``services.image_sources``),
    or ``None`` for a picker that only has the file chooser.
    ``on_select(data)`` receives the raw bytes of the chosen image, whether it
    came from the file chooser, the shortcut or a search result.
    """
    search = image_search.search if image_search is not None else None
    shortcut = image_search.shortcut if image_search is not None else None

    cell_width, cell_height = _tile_cell_size()

    # Just wide enough for TILES_PER_LINE tiles side by side...
    content_width = (
        TILES_PER_LINE * cell_width
        + (TILES_PER_LINE - 1) * TILE_SPACING
    )
    # ...and the results list is at most MAX_RESULT_ROWS rows tall.
    results_height = (
        MAX_RESULT_ROWS * cell_height
        + (MAX_RESULT_ROWS - 1) * TILE_SPACING
    )

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(title)
    # Locked: the width is the content's (set below) and the height follows
    # the content, up to the results list's maximum.
    window.set_resizable(False)

    # ``search_id`` tells the searches apart: only the latest one is shown.
    state = {
        "closed": False,
        "search_id": 0,
        "picking": False,
        "alive": 0,
        "fingerprints": [],
    }
    gate = threading.BoundedSemaphore(MAX_CONCURRENT_LOADS)

    def on_close_request(_window):
        state["closed"] = True
        return False

    window.connect("close-request", on_close_request)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    toasts = Adw.ToastOverlay()

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    # Fixed width, with or without a search below the buttons.
    content.set_size_request(content_width, -1)
    content.set_hexpand(False)

    def choose(data):
        if state["closed"]:
            return
        window.close()
        on_select(data)

    # -- choose a file ---------------------------------------------------

    select_button = Gtk.Button()
    select_button.set_child(
        Adw.ButtonContent(
            icon_name="document-open-symbolic",
            label="Select image…",
        )
    )
    select_button.add_css_class("suggested-action")
    select_button.set_hexpand(True)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    buttons.set_homogeneous(True)
    buttons.append(select_button)
    content.append(buttons)

    def on_file_chosen(dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed

        path = file.get_path() if file is not None else None

        try:
            if not path:
                raise OSError("not a local file")

            if Path(path).stat().st_size > MAX_FILE_SIZE:
                toasts.add_toast(Adw.Toast.new("That image is too large"))
                return

            data = Path(path).read_bytes()

            if not _is_image(data):
                raise ValueError("not an image")

        except (OSError, ValueError):
            toasts.add_toast(Adw.Toast.new("Could not open that image"))
            return

        choose(data)

    def on_select_clicked(_button):
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        image_filter.add_mime_type("image/*")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)

        dialog = Gtk.FileDialog()
        dialog.set_title("Select an image")
        dialog.set_filters(filters)
        dialog.set_default_filter(image_filter)
        dialog.open(window, None, on_file_chosen)

    select_button.connect("clicked", on_select_clicked)

    # -- the shortcut (a track's album cover) ----------------------------

    if shortcut is not None:
        shortcut_button = Gtk.Button()
        shortcut_button.set_child(
            Adw.ButtonContent(
                icon_name="media-optical-symbolic",
                label=image_search.shortcut_label,
            )
        )
        shortcut_button.set_hexpand(True)
        # Until the library has been looked through, and for good when the
        # album has no cover.
        shortcut_button.set_sensitive(False)
        shortcut_button.set_tooltip_text("Looking for the album's cover…")
        buttons.append(shortcut_button)

        shortcut_data = {"bytes": None}

        def shortcut_ready(data):
            if state["closed"]:
                return GLib.SOURCE_REMOVE

            if data and _is_image(data):
                shortcut_data["bytes"] = data
                shortcut_button.set_sensitive(True)
                shortcut_button.set_tooltip_text(shortcut.detail or None)
            else:
                shortcut_button.set_tooltip_text(
                    "This album has no cover to use"
                )

            return GLib.SOURCE_REMOVE

        def load_shortcut():
            try:
                data = shortcut.loader()
            except Exception:
                data = None

            GLib.idle_add(shortcut_ready, data)

        shortcut_button.connect(
            "clicked",
            lambda _button: choose(shortcut_data["bytes"]),
        )
        threading.Thread(target=load_shortcut, daemon=True).start()

    # -- suggestions from other sites ------------------------------------

    suggestions = image_search.suggestions if image_search is not None else []

    if suggestions:
        suggestions_heading = Gtk.Label(label="Suggestions", xalign=0)
        suggestions_heading.add_css_class("heading")

        suggestions_flow = Gtk.FlowBox()
        suggestions_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        suggestions_flow.set_homogeneous(True)
        suggestions_flow.set_min_children_per_line(TILES_PER_LINE)
        suggestions_flow.set_max_children_per_line(TILES_PER_LINE)
        suggestions_flow.set_column_spacing(TILE_SPACING)
        suggestions_flow.set_row_spacing(TILE_SPACING)
        suggestions_flow.set_halign(Gtk.Align.START)

        suggestions_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        suggestions_box.append(suggestions_heading)
        suggestions_box.append(suggestions_flow)
        content.append(suggestions_box)

        def suggestion_ready(tile, data, png):
            if state["closed"]:
                return GLib.SOURCE_REMOVE

            texture = texture_from_png(png) if png else None

            if texture is None:
                tile.show_empty()
            else:
                tile.show_image(data, texture)

            return GLib.SOURCE_REMOVE

        def load_suggestion(tile):
            with gate:
                if state["closed"]:
                    return

                try:
                    data = tile.source.loader()
                except Exception:
                    data = None

            png = thumbnail_png(data, TILE_SIZE) if data else None

            GLib.idle_add(suggestion_ready, tile, data if png else None, png)

        def on_suggestion_clicked(tile):
            if tile.data:
                choose(tile.data)

        for source in suggestions:
            tile = _SourceTile(source)
            tile.connect("clicked", on_suggestion_clicked)
            suggestions_flow.append(tile)
            threading.Thread(
                target=load_suggestion,
                args=(tile,),
                daemon=True,
            ).start()

    # -- the search ------------------------------------------------------

    entry = None

    if search is not None:
        entry = Gtk.SearchEntry()
        entry.set_placeholder_text("Search YouTube…")
        entry.set_text(image_search.query)

        hint = Gtk.Label(label=image_search.hint, xalign=0, wrap=True)
        # Wrap to the window's width, never widen it.
        hint.set_max_width_chars(1)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_visible(bool(image_search.hint))

        search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        search_box.append(entry)
        search_box.append(hint)
        content.append(search_box)

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_min_children_per_line(TILES_PER_LINE)
        flow.set_max_children_per_line(TILES_PER_LINE)
        flow.set_column_spacing(TILE_SPACING)
        flow.set_row_spacing(TILE_SPACING)
        flow.set_halign(Gtk.Align.START)
        flow.set_valign(Gtk.Align.START)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # As tall as its results, up to MAX_RESULT_ROWS rows; beyond that the
        # list scrolls.
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(results_height)
        scrolled.set_vexpand(True)
        scrolled.set_child(flow)

        message = Adw.StatusPage()
        message.add_css_class("compact")
        message.set_vexpand(True)

        stack = Gtk.Stack()
        stack.set_vexpand(True)
        stack.add_named(message, "message")
        stack.add_named(scrolled, "results")
        content.append(stack)

        def show_message(icon, heading, description=""):
            message.set_icon_name(icon)
            message.set_title(heading)
            message.set_description(description)
            stack.set_visible_child_name("message")

        def current(search_id):
            return not state["closed"] and search_id == state["search_id"]

        def clear_results():
            child = flow.get_first_child()

            while child is not None:
                next_child = child.get_next_sibling()
                flow.remove(child)
                child = next_child

        def drop(tile):
            """Leave a result out; with none left, say so."""
            tile.remove_from_results()
            state["alive"] -= 1

            if state["alive"] <= 0:
                show_message(
                    "system-search-symbolic",
                    "No results",
                    "Try different words.",
                )

        def finish_tile(search_id, tile, png, fingerprint):
            if not current(search_id):
                return GLib.SOURCE_REMOVE

            texture = texture_from_png(png) if png else None

            if texture is None:
                drop(tile)
                return GLib.SOURCE_REMOVE

            if image_search.dedupe:
                if any(
                        (fingerprint ^ other).bit_count() <= SAME_PICTURE_BITS
                        for other in state["fingerprints"]
                ):
                    drop(tile)
                    return GLib.SOURCE_REMOVE

                state["fingerprints"].append(fingerprint)

            tile.show_thumbnail(texture)
            return GLib.SOURCE_REMOVE

        def load_tile(search_id, tile):
            # Only a few at a time; a search that was replaced while this
            # waited for its turn is not worth fetching for.
            with gate:
                if not current(search_id):
                    return

                try:
                    data = tile.result.load_thumbnail()
                except Exception:
                    data = None

            png = thumbnail_png(data, TILE_SIZE) if data else None

            try:
                fingerprint = _fingerprint(png) if png else 0
            except Exception:
                png, fingerprint = None, 0

            GLib.idle_add(finish_tile, search_id, tile, png, fingerprint)

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

            state["alive"] = len(results)
            state["fingerprints"] = []

            for result in results:
                tile = _Tile(result)
                tile.connect("clicked", on_tile_clicked)
                flow.append(tile)
                threading.Thread(
                    target=load_tile,
                    args=(search_id, tile),
                    daemon=True,
                ).start()

            scrolled.get_vadjustment().set_value(0)
            stack.set_visible_child_name("results")
            return GLib.SOURCE_REMOVE

        def search_failed(search_id, text):
            if current(search_id):
                show_message("dialog-error-symbolic", "Search failed", text)

            return GLib.SOURCE_REMOVE

        def search_worker(search_id, query):
            try:
                results = search(query)
            except Exception as error:
                GLib.idle_add(
                    search_failed,
                    search_id,
                    str(error) or "The search failed.",
                )
            else:
                GLib.idle_add(show_results, search_id, results)

        def run_search(*_args):
            query = entry.get_text().strip()

            if not query:
                return

            state["search_id"] += 1
            show_message("system-search-symbolic", "Searching…")

            threading.Thread(
                target=search_worker,
                args=(state["search_id"], query),
                daemon=True,
            ).start()

        # -- picking a result --------------------------------------------

        def fetch_full(tile):
            try:
                data = tile.result.load_full()
            except Exception:
                data = None

            if data and not _is_image(data):
                data = None

            GLib.idle_add(full_fetched, tile, data)

        def full_fetched(tile, data):
            state["picking"] = False

            if state["closed"]:
                return GLib.SOURCE_REMOVE

            tile.set_busy(False)

            if data is None:
                toasts.add_toast(Adw.Toast.new("Could not load that image"))
            else:
                choose(data)

            return GLib.SOURCE_REMOVE

        def on_tile_clicked(tile):
            # The thumbnail is small: the full-size picture is fetched now.
            if state["picking"]:
                return

            state["picking"] = True
            tile.set_busy(True)
            threading.Thread(
                target=fetch_full,
                args=(tile,),
                daemon=True,
            ).start()

        entry.connect("activate", run_search)

        if entry.get_text().strip():
            show_message("system-search-symbolic", "Searching…")
        else:
            show_message(
                "system-search-symbolic",
                "Search YouTube",
                "Type what to look for and press Enter.",
            )

    # -- window ----------------------------------------------------------

    escape = Gtk.EventControllerKey()

    def on_key_pressed(_controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False

    escape.connect("key-pressed", on_key_pressed)
    window.add_controller(escape)

    toasts.set_child(content)
    toolbar.set_content(toasts)
    window.set_content(toolbar)
    window.present()

    if entry is not None:
        entry.grab_focus()

        # What the search starts with is searched at once.
        if entry.get_text().strip():
            run_search()
