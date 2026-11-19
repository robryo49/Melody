"""Shared grid used by the Artists, Albums and Playlists overview pages.

The three pages only differ by the data they show (and what happens when a
card is opened / played), so the layout lives here once:

* ``CollectionCard``  - one tile: cover, title, subtitle, hover play button.
* ``CollectionGrid``  - scrollable, wrapping grid of cards.
"""

import os
from functools import lru_cache

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, Gtk, Pango

from melody.services.collection_actions import attach_context_menu
from melody.widgets.cover_style import (
    COVER_RADIUS,
    install_cover_css as _install_css,
)

COVER_SIZE = 160
GRID_SPACING = 24
GRID_MARGIN = 24


@lru_cache(maxsize=512)
def _load_texture(path, size, mtime):
    """Load a cover once, centre-cropped to a square and scaled down.

    Cropping here (rather than relying on ``Gtk.Picture``'s cover fit) lets the
    card show the texture through a fixed-size ``Gtk.Image``, so the image's
    own dimensions can never influence the card width.
    """
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        width, height = pixbuf.get_width(), pixbuf.get_height()
        side = min(width, height)
        pixbuf = pixbuf.new_subpixbuf(
            (width - side) // 2, (height - side) // 2, side, side
        )
        if side > size:
            pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:
        return None


def load_cover_texture(path, size=COVER_SIZE * 2):
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return _load_texture(path, size, mtime)


class CollectionCard(Gtk.Box):
    """A single cover tile: square, or round with ``round_cover``."""

    def __init__(
            self,
            item,
            title,
            subtitle,
            artwork_path,
            placeholder_icon,
            play_tooltip,
            on_play,
            on_edit=None,
            on_open=None,
            on_delete=None,
            round_cover=False,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        _install_css()

        self.item = item
        self._hovered = False
        self._playing = False
        self._play_tooltip = play_tooltip

        self.set_size_request(COVER_SIZE, -1)
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)

        # --- cover ------------------------------------------------------
        cover = Gtk.Overlay()
        cover.set_size_request(COVER_SIZE, COVER_SIZE)
        cover.set_overflow(Gtk.Overflow.HIDDEN)
        # No "card" class: it paints a background/border/shadow behind the
        # artwork, which peeks out of artwork that has its own rounded corners.
        # A round cover is cut to its circle by the same clipping.
        cover.add_css_class("round-cover" if round_cover else "cover-art")

        texture = load_cover_texture(artwork_path)
        if texture is not None:
            image = Gtk.Image.new_from_paintable(texture)
            image.set_pixel_size(COVER_SIZE)
            cover.set_child(image)
        else:
            icon = Gtk.Image.new_from_icon_name(placeholder_icon)
            icon.set_pixel_size(64)
            icon.add_css_class("dim-label")
            cover.set_child(icon)
            cover.add_css_class("cover-placeholder")

        self.play_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_button.add_css_class("circular")
        self.play_button.add_css_class("osd")
        self.play_button.set_halign(Gtk.Align.END)
        self.play_button.set_valign(Gtk.Align.END)
        # In the corner of a circle the button would be cut off: bring it in.
        play_margin = 24 if round_cover else 8
        self.play_button.set_margin_end(play_margin)
        self.play_button.set_margin_bottom(play_margin)
        self.play_button.set_tooltip_text(play_tooltip)
        self.play_button.set_visible(False)
        self.play_button.connect("clicked", lambda _b: on_play(self.item))
        cover.add_overlay(self.play_button)

        # --- labels -----------------------------------------------------
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_max_width_chars(1)  # let the card width decide
        title_label.set_tooltip_text(title)
        title_label.add_css_class("heading")

        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle_label.set_max_width_chars(1)
        subtitle_label.set_tooltip_text(subtitle)
        subtitle_label.add_css_class("dim-label")
        subtitle_label.add_css_class("caption")

        self.append(cover)
        self.append(title_label)
        self.append(subtitle_label)

        # --- hover ------------------------------------------------------
        motion = Gtk.EventControllerMotion.new()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        # --- right-click menu -------------------------------------------
        if on_edit is not None:
            def menu_entries():
                entries = [
                    (
                        "Pause" if self._playing else "Play",
                        lambda: on_play(self.item),
                    ),
                ]

                if on_open is not None:
                    entries.append(("Open", lambda: on_open(self.item)))

                entries.append(("Edit…", lambda: on_edit(self.item)))

                if on_delete is not None:
                    entries.append(
                        ("Delete", lambda: on_delete(self.item))
                    )

                return entries

            attach_context_menu(self, menu_entries)

    def _on_enter(self, _controller, _x, _y):
        self._hovered = True
        self._sync_play_button()

    def _on_leave(self, _controller):
        self._hovered = False
        self._sync_play_button()

    def _sync_play_button(self):
        self.play_button.set_visible(self._playing or self._hovered)

    def set_playing(self, playing):
        if playing != self._playing:
            self._playing = playing
            self.play_button.set_icon_name(
                "media-playback-pause-symbolic"
                if playing
                else "media-playback-start-symbolic"
            )
            self.play_button.set_tooltip_text(
                self._play_tooltip.replace("Play", "Pause")
                if playing
                else self._play_tooltip
            )
        self._sync_play_button()


class CollectionGrid(Gtk.ScrolledWindow):
    """Scrollable grid of ``CollectionCard`` tiles that wraps to the width."""

    def __init__(self, on_open, placeholder_icon, round_covers=False):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)

        self.cards = []
        self._on_open = on_open
        self._placeholder_icon = placeholder_icon
        self._round_covers = round_covers

        self.flow_box = Gtk.FlowBox()
        self.flow_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow_box.set_homogeneous(True)
        self.flow_box.set_min_children_per_line(1)
        self.flow_box.set_max_children_per_line(64)
        self.flow_box.set_column_spacing(GRID_SPACING)
        self.flow_box.set_row_spacing(GRID_SPACING)
        self.flow_box.set_halign(Gtk.Align.START)
        self.flow_box.set_valign(Gtk.Align.START)
        self.flow_box.set_margin_start(GRID_MARGIN)
        self.flow_box.set_margin_end(GRID_MARGIN)
        self.flow_box.set_margin_top(12)
        self.flow_box.set_margin_bottom(GRID_MARGIN)
        self.flow_box.connect("child-activated", self._on_child_activated)

        self.set_child(self.flow_box)

    def populate(
            self,
            items,
            describe,
            on_play,
            play_tooltip,
            on_edit=None,
            open_in_menu=False,
            on_delete=None,
    ):
        """Rebuild the grid.

        ``describe(item)`` must return ``(title, subtitle, artwork_path)``.
        ``on_play(item)`` is called when a card's play/pause button is hit.
        ``on_edit(item)``, if given, adds an "Edit…" entry to the cards'
        right-click menu (which also has Play/Pause). With ``open_in_menu``
        the menu also gets an "Open" entry, which does what clicking the
        card does. With ``on_delete(item)`` it gets a "Delete" entry too.
        """
        self.flow_box.remove_all()
        self.cards = []

        for item in items:
            title, subtitle, artwork_path = describe(item)
            card = CollectionCard(
                item,
                title,
                subtitle,
                artwork_path,
                self._placeholder_icon,
                play_tooltip,
                on_play,
                on_edit,
                self._on_open if open_in_menu else None,
                on_delete,
                round_cover=self._round_covers,
            )
            self.flow_box.append(card)
            self.cards.append(card)

    def update_playing(self, is_playing):
        """``is_playing(item) -> bool`` toggles every card's play button."""
        for card in self.cards:
            card.set_playing(bool(is_playing(card.item)))

    def _on_child_activated(self, _flow_box, child):
        card = child.get_child()
        if isinstance(card, CollectionCard):
            self._on_open(card.item)