"""The popup that lets an edit window pick a new image.

It offers a "Select image…" button (file chooser) and, below it, a few
suggested images, each labelled with where it comes from. Suggestions are
fetched in background threads, so the window opens instantly; a suggestion
that turns out to have nothing simply disappears.

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

TILE_SIZE = 96
TILE_SPACING = 12
TILES_PER_LINE = 4
MAX_FILE_SIZE = 20 * 1024 * 1024


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


class _Tile(Gtk.Button):
    """One suggestion: a thumbnail with its source's name underneath."""

    def __init__(self, source):
        super().__init__()

        self.source = source
        self.data = None

        self.add_css_class("flat")
        self.set_tooltip_text(
            f"Use the image from {source.name}"
            if not source.detail
            else f"Use the image from {source.name} {source.detail}"
        )

        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        spinner.start()

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_can_shrink(True)

        self._stack = Gtk.Stack()
        self._stack.add_css_class("card")
        self._stack.set_overflow(Gtk.Overflow.HIDDEN)
        self._stack.set_size_request(TILE_SIZE, TILE_SIZE)
        self._stack.add_named(spinner, "loading")
        self._stack.add_named(self._picture, "image")

        # Shown when the source has nothing to offer, so the row of
        # suggestions always has the same number of tiles.
        empty_icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
        empty_icon.set_pixel_size(24)

        empty_label = Gtk.Label(label="No suggestion from this source")
        empty_label.set_wrap(True)
        empty_label.set_justify(Gtk.Justification.CENTER)
        empty_label.set_max_width_chars(12)

        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        empty.set_halign(Gtk.Align.CENTER)
        empty.set_valign(Gtk.Align.CENTER)
        empty.add_css_class("dim-label")
        empty.add_css_class("caption")
        empty.append(empty_icon)
        empty.append(empty_label)

        self._stack.add_named(empty, "empty")
        self._stack.set_visible_child_name("loading")

        name = Gtk.Label(label=source.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(1)
        name.add_css_class("heading")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(TILE_SIZE, -1)
        box.append(self._stack)
        box.append(name)

        if source.detail:
            detail = Gtk.Label(label=source.detail, xalign=0)
            detail.set_ellipsize(Pango.EllipsizeMode.END)
            detail.set_max_width_chars(1)
            detail.add_css_class("dim-label")
            detail.add_css_class("caption")
            box.append(detail)

        self.set_child(box)
        self.set_sensitive(False)

    def show_empty(self):
        self.set_tooltip_text(f"No suggestion from {self.source.name}")
        self._stack.set_visible_child_name("empty")

    def show_result(self, data, texture):
        self.data = data
        self._picture.set_paintable(texture)
        self._stack.set_visible_child_name("image")
        self.set_sensitive(True)


def show_image_picker(parent, sources, on_select, *, title="Change image"):
    """Open the picker.

    ``sources`` is a list of ``ImageSource`` (see ``services.image_sources``).
    ``on_select(data)`` receives the raw bytes of the chosen image, whether it
    came from a suggestion or from the file chooser.
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(title)
    window.set_resizable(False)

    state = {"closed": False}

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
    # Fixed width: the window must not jump around as suggestions come in
    # or turn out empty.
    content.set_size_request(
        TILES_PER_LINE * (TILE_SIZE + 12) + (TILES_PER_LINE - 1) * TILE_SPACING,
        -1,
    )

    # -- choose a file ---------------------------------------------------

    select_button = Gtk.Button()
    select_button.set_child(
        Adw.ButtonContent(
            icon_name="document-open-symbolic",
            label="Select image…",
        )
    )
    select_button.add_css_class("suggested-action")
    content.append(select_button)

    def choose(data):
        if state["closed"]:
            return
        window.close()
        on_select(data)

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

    # -- suggestions -----------------------------------------------------

    if sources:
        heading = Gtk.Label(label="Suggestions", xalign=0)
        heading.add_css_class("caption-heading")

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(TILES_PER_LINE)
        flow.set_column_spacing(TILE_SPACING)
        flow.set_row_spacing(TILE_SPACING)
        flow.set_halign(Gtk.Align.START)
        flow.set_valign(Gtk.Align.START)

        suggestions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        suggestions.append(heading)
        suggestions.append(flow)
        content.append(suggestions)

        def finish(tile, data, png):
            if state["closed"]:
                return GLib.SOURCE_REMOVE

            texture = texture_from_png(png) if png else None

            if texture is None:
                tile.show_empty()
            else:
                tile.show_result(data, texture)

            return GLib.SOURCE_REMOVE

        def work(tile):
            try:
                data = tile.source.loader()
            except Exception:
                data = None

            # Exactly the tile's size: a picture's natural size would
            # otherwise stretch the tile.
            png = thumbnail_png(data, TILE_SIZE) if data else None
            GLib.idle_add(finish, tile, data if png else None, png)

        for source in sources:
            tile = _Tile(source)
            tile.connect(
                "clicked",
                lambda button: choose(button.data),
            )
            flow.append(tile)
            threading.Thread(target=work, args=(tile,), daemon=True).start()

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
