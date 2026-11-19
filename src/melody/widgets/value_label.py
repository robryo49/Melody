"""A plain, wrapping text label for the big values in a page header."""

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango


def value_label(css_classes=()):
    """A left-aligned label that wraps instead of widening its page."""
    label = Gtk.Label(xalign=0)
    label.set_halign(Gtk.Align.START)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)

    for css_class in css_classes:
        label.add_css_class(css_class)

    return label
