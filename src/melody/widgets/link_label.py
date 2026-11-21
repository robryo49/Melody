"""Make a label clickable, like a link: a pointer cursor and an underline
while the pointer is over it."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

_CSS = """
.link-label:hover {
    text-decoration: underline;
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


def make_link(label, on_click):
    """Call ``on_click()`` when ``label`` is clicked.

    The label only reacts over its text, so it should not be stretched to
    fill the row (``set_halign(Gtk.Align.START)``).
    """
    _install_css()

    label.add_css_class("link-label")
    label.set_cursor_from_name("pointer")

    def on_released(_gesture, _n_press, x, y):
        # Released with the pointer moved away: not a click.
        if 0 <= x <= label.get_width() and 0 <= y <= label.get_height():
            on_click()

    gesture = Gtk.GestureClick.new()
    gesture.set_button(Gdk.BUTTON_PRIMARY)
    gesture.connect("released", on_released)
    label.add_controller(gesture)
