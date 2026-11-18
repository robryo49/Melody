"""A text value with a small edit button right next to it.

The button is invisible until the pointer is over the value (or the button
has keyboard focus), so it never clutters the page but is always reachable.
It is always laid out, so revealing it never shifts the surrounding content.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk, Pango

_CSS = """
.editable-value-button {
    opacity: 0;
    transition: opacity 150ms ease;
}
.editable-value-button.revealed {
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
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


class EditableValue(Gtk.Box):
    """Label + edit button. ``on_edit()`` is called when the button is hit."""

    def __init__(self, on_edit, tooltip, css_classes=()):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        _install_css()

        self._on_edit = on_edit
        self._hovered = False
        self._focused = False

        self.set_halign(Gtk.Align.START)

        self.label = Gtk.Label(xalign=0)
        self.label.set_wrap(True)
        self.label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        for css_class in css_classes:
            self.label.add_css_class(css_class)

        self.button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        self.button.add_css_class("flat")
        self.button.add_css_class("circular")
        self.button.add_css_class("editable-value-button")
        self.button.set_valign(Gtk.Align.CENTER)
        self.button.set_tooltip_text(tooltip)
        self.button.set_can_target(False)
        self.button.connect("clicked", lambda _button: self._on_edit())

        self.append(self.label)
        self.append(self.button)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        focus = Gtk.EventControllerFocus.new()
        focus.connect("enter", self._on_focus_enter)
        focus.connect("leave", self._on_focus_leave)
        self.button.add_controller(focus)

    # -- text ------------------------------------------------------------

    def set_text(self, text):
        self.label.set_text(text or "")

    def get_text(self):
        return self.label.get_text()

    # -- reveal ----------------------------------------------------------

    def _on_enter(self, _controller, _x, _y):
        self._hovered = True
        self._sync()

    def _on_leave(self, _controller):
        self._hovered = False
        self._sync()

    def _on_focus_enter(self, _controller):
        self._focused = True
        self._sync()

    def _on_focus_leave(self, _controller):
        self._focused = False
        self._sync()

    def _sync(self):
        revealed = self._hovered or self._focused
        if revealed:
            self.button.add_css_class("revealed")
        else:
            self.button.remove_css_class("revealed")
        # A hidden button must not swallow clicks meant for what is under it.
        self.button.set_can_target(revealed)