"""A number typed into a small text field, in a page's header.

Used by the Recent page ("Show the last [30] tracks added") and the Most
played page ("Show the [30] most played tracks"). The number is applied when
Enter is pressed or the field is left (by clicking anywhere else, say);
anything that is not a whole number of at least 1 is put back to the number in
use.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

# How many digits the field has room for. Longer numbers scroll inside it.
ENTRY_WIDTH_CHARS = 3

_CSS = """
entry.count-control {
    min-width: 0;
    padding-left: 6px;
    padding-right: 6px;
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


class CountControl(Gtk.Box):
    """``[before] [number] [after]``, e.g. "Show the last" 30 "tracks added".

    ``on_changed(count)`` is called with the new number (an int) whenever a
    different, valid one is entered.
    """

    def __init__(self, count, on_changed, *, before, after, tooltip=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        _install_css()

        self._count = max(1, int(count))
        self._on_changed = on_changed
        self._click_away = None

        self.set_valign(Gtk.Align.CENTER)

        before_label = Gtk.Label(label=before)

        self.entry = Gtk.Entry()
        self.entry.add_css_class("count-control")
        self.entry.set_text(str(self._count))
        self.entry.set_width_chars(ENTRY_WIDTH_CHARS)
        self.entry.set_max_width_chars(ENTRY_WIDTH_CHARS)
        self.entry.set_hexpand(False)
        self.entry.set_max_length(6)
        self.entry.set_alignment(0.5)
        self.entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.entry.set_tooltip_text(tooltip)
        self.entry.connect("activate", self._commit)

        # Leaving the field applies what was typed, like pressing Enter.
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._commit)
        self.entry.add_controller(focus)

        after_label = Gtk.Label(label=after)

        self.append(before_label)
        self.append(self.entry)
        self.append(after_label)

        # Clicking outside the field must leave it; that needs the window.
        self.connect("notify::root", self._on_root_changed)

    # -- clicking outside ------------------------------------------------

    def _on_root_changed(self, *_args):
        root = self.get_root()

        if root is None or self._click_away is not None:
            return

        # Seen before any widget handles the click, and it never claims it,
        # so the click still does whatever it was meant to.
        gesture = Gtk.GestureClick.new()
        gesture.set_button(0)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_pressed_anywhere, root)
        root.add_controller(gesture)
        self._click_away = gesture

    def _inside_entry(self, widget):
        while widget is not None:
            if widget is self.entry:
                return True
            widget = widget.get_parent()

        return False

    def _on_pressed_anywhere(self, _gesture, _n_press, x, y, root):
        # Only matters while the field is being edited.
        if not self._inside_entry(root.get_focus()):
            return

        if self._inside_entry(root.pick(x, y, Gtk.PickFlags.DEFAULT)):
            return

        # Dropping the focus ends the editing (and applies the number, see
        # ``_commit``).
        root.set_focus(None)

    # -- applying --------------------------------------------------------

    def _commit(self, *_args):
        try:
            value = int(self.entry.get_text().strip())
        except ValueError:
            value = 0

        if value < 1:
            # Not a usable number: go back to the one in use.
            self.entry.set_text(str(self._count))
            return

        # Shown the way it is stored ("030" becomes "30").
        self.entry.set_text(str(value))

        if value != self._count:
            self._count = value
            self._on_changed(value)
