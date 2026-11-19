"""A cover with an edit button floating over its bottom-right corner."""

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


def with_edit_button(cover, on_edit, tooltip="Change image", margin=8):
    """Wrap ``cover`` in an overlay carrying a round edit button.

    The button is only visible while the cover is hovered, the same way the
    play button is in the grid views. ``on_edit()`` is called when it is
    clicked. The overlay is laid out exactly like ``cover`` was.

    ``margin`` is how far the button sits from the corner. A round cover
    needs more, so that the button stays on the picture.
    """
    overlay = Gtk.Overlay()
    overlay.set_valign(cover.get_valign())
    overlay.set_halign(cover.get_halign())
    overlay.set_child(cover)

    button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
    button.add_css_class("circular")
    button.add_css_class("osd")
    button.set_halign(Gtk.Align.END)
    button.set_valign(Gtk.Align.END)
    button.set_margin_end(margin)
    button.set_margin_bottom(margin)
    button.set_tooltip_text(tooltip)
    button.set_visible(False)
    button.connect("clicked", lambda _button: on_edit())
    overlay.add_overlay(button)

    motion = Gtk.EventControllerMotion.new()
    motion.connect("enter", lambda _controller, _x, _y: button.set_visible(True))
    motion.connect("leave", lambda _controller: button.set_visible(False))
    overlay.add_controller(motion)

    return overlay
