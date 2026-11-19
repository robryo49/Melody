"""The styles shared by every picture Melody shows as a cover.

* ``cover-art``: a square cover with rounded corners.
* ``round-cover``: a round one (an artist's picture). Give the widget
  ``set_overflow(Gtk.Overflow.HIDDEN)`` too, so the picture is cut to the
  circle.
* ``cover-placeholder``: the soft background behind the icon shown when
  there is no picture.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

COVER_RADIUS = 12

_CSS = f"""
.cover-art {{
    border-radius: {COVER_RADIUS}px;
}}
.round-cover {{
    border-radius: 9999px;
}}
.cover-placeholder {{
    background-color: alpha(currentColor, 0.08);
}}
"""
_css_installed = False


def install_cover_css():
    """Register the cover styles once for the whole display."""
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
