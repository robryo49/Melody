"""A labelled slider for a number of decibels that may also be "none".

Used by the edit window for a track's ReplayGain: dragging the slider gives
the track a gain, and the clear button takes it away again (the value then
reads "None", and the slider is dimmed).
"""

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from melody.services.replaygain import MAX_GAIN, format_gain

STEP = 0.1
PAGE_STEP = 1.0


class GainSlider(Gtk.Box):
    """``value`` is the gain in dB, or None when there is none.

    A value that was there to begin with is kept exactly as it was until the
    slider is moved (the slider itself works in steps of ``STEP``).
    """

    def __init__(self, label, initial=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self.value = initial
        self._syncing = False
        self._callbacks = []

        caption = Gtk.Label(label=label, xalign=0)
        caption.add_css_class("caption-heading")

        self.scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            -MAX_GAIN,
            MAX_GAIN,
            STEP,
        )
        self.scale.set_increments(STEP, PAGE_STEP)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        # A mark where nothing is changed.
        self.scale.add_mark(0.0, Gtk.PositionType.BOTTOM, None)
        self.scale.connect("value-changed", self._on_scale_changed)

        self.value_label = Gtk.Label(xalign=1)
        self.value_label.set_width_chars(9)
        self.value_label.add_css_class("dim-label")

        self.clear_button = Gtk.Button.new_from_icon_name(
            "edit-clear-symbolic"
        )
        self.clear_button.add_css_class("flat")
        self.clear_button.set_valign(Gtk.Align.CENTER)
        self.clear_button.set_tooltip_text("Remove the tag")
        self.clear_button.connect("clicked", self._on_clear_clicked)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(self.scale)
        row.append(self.value_label)
        row.append(self.clear_button)

        self.append(caption)
        self.append(row)

        self._sync()

    def connect_changed(self, callback):
        self._callbacks.append(callback)

    def _sync(self):
        """Show ``value``: the slider on it, its text, and the clear button
        and dimming that go with having one or not."""
        has_value = self.value is not None

        self._syncing = True
        self.scale.set_value(self.value if has_value else 0.0)
        self._syncing = False

        self.value_label.set_text(
            f"{format_gain(self.value)} dB" if has_value else "None"
        )
        self.clear_button.set_sensitive(has_value)

        if has_value:
            self.scale.remove_css_class("dim-label")
        else:
            self.scale.add_css_class("dim-label")

    def _notify(self):
        for callback in self._callbacks:
            callback()

    def _on_scale_changed(self, scale):
        if self._syncing:
            return

        self.value = round(scale.get_value(), 1)
        self._sync()
        self._notify()

    def _on_clear_clicked(self, _button):
        self.value = None
        self._sync()
        self._notify()
