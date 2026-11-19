"""Choose where a track starts and ends.

``TimeRangeSelector`` is two editable time fields (start on the left, end on
the right) sitting right above a ``RangeSlider``: a slider like the player's
seek bar, but with two handles. The left handle is always the start and the
right one always the end: if the handles are dragged past each other (or a
time is typed past the other one), they simply swap roles.

The fields and the slider always show the same values.
"""

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

_TIME_PATTERN = re.compile(r"^(?:(?:(\d+):)?(\d+):)?(\d+(?:[.,]\d*)?)$")
_TAU = 6.283185307179586


def format_time(seconds, with_hours=False):
    """``1:05.250``, or ``0:01:05.250`` when ``with_hours``."""
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)

    if with_hours:
        return f"{hours}:{minutes:02}:{secs:02}.{millis:03}"

    return f"{minutes}:{secs:02}.{millis:03}"


def parse_time(text):
    """Seconds from ``s``, ``s.mmm``, ``m:ss``, ``m:ss.mmm`` or
    ``h:mm:ss.mmm``. ``None`` when the text is not a time."""
    match = _TIME_PATTERN.match(text.strip())

    if match is None:
        return None

    hours, minutes, seconds = match.groups()
    return (
        int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + float(seconds.replace(",", "."))
    )


class RangeSlider(Gtk.DrawingArea):
    """A slider with two handles, drawn like the player's seek bar."""

    PADDING = 14
    HANDLE_RADIUS = 10

    def __init__(self):
        super().__init__()

        self.duration = 0.0
        # Always sorted: [start, end].
        self._values = [0.0, 0.0]
        self._active = None
        self._origin_x = 0.0
        self._callbacks = []

        self.set_hexpand(True)
        self.set_focusable(True)
        self.set_content_height(26)
        self.set_size_request(80, 26)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    # -- public ----------------------------------------------------------

    def connect_changed(self, callback):
        """``callback(start, end)`` runs when the *user* moves a handle."""
        self._callbacks.append(callback)

    def get_range(self):
        return self._values[0], self._values[1]

    def set_duration(self, duration):
        self.duration = max(0.0, float(duration))
        self._values = [self._clamp(value) for value in self._values]
        self.queue_draw()

    def set_range(self, first, second):
        """Move the handles (silently). The order given doesn't matter."""
        first = self._clamp(first)
        second = self._clamp(second)
        self._values = [min(first, second), max(first, second)]
        self.queue_draw()

    # -- geometry --------------------------------------------------------

    def _clamp(self, value):
        return max(0.0, min(float(value), self.duration))

    def _x_from_value(self, value, width):
        start_x = self.PADDING
        end_x = max(start_x + 1, width - self.PADDING)
        fraction = value / self.duration if self.duration > 0 else 0.0
        return start_x + (end_x - start_x) * fraction

    def _value_from_x(self, x):
        start_x = self.PADDING
        end_x = max(start_x + 1, self.get_width() - self.PADDING)
        fraction = max(0.0, min(1.0, (x - start_x) / (end_x - start_x)))
        return fraction * self.duration

    # -- dragging --------------------------------------------------------

    def _on_drag_begin(self, gesture, x, y):
        if self.duration <= 0 or not self.get_sensitive():
            self._active = None
            return

        # Taking focus makes a time field being typed in commit its text
        # before the handle moves.
        self.grab_focus()

        width = self.get_width()
        start_distance = abs(x - self._x_from_value(self._values[0], width))
        end_distance = abs(x - self._x_from_value(self._values[1], width))

        if start_distance < end_distance:
            self._active = 0
        elif end_distance < start_distance:
            self._active = 1
        else:
            # Both handles are in the same place: take the one on the side
            # the pointer is on.
            self._active = (
                0 if x <= self._x_from_value(self._values[0], width) else 1
            )

        self._origin_x = x
        self._move_active(self._value_from_x(x))

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if self._active is None:
            return

        self._move_active(self._value_from_x(self._origin_x + offset_x))

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self._active = None
        self.queue_draw()

    def _move_active(self, value):
        value = self._clamp(value)

        if self._values[self._active] == value:
            return

        self._values[self._active] = value

        # Handles that cross swap roles, so the left one stays the start.
        if self._values[0] > self._values[1]:
            self._values.reverse()
            self._active = 1 - self._active

        self.queue_draw()

        for callback in self._callbacks:
            callback(self._values[0], self._values[1])

    # -- drawing ---------------------------------------------------------

    def _draw(self, area, cr, width, height):
        start_x = self.PADDING
        end_x = max(start_x + 1, width - self.PADDING)
        center_y = height / 2
        low_x = self._x_from_value(self._values[0], width)
        high_x = self._x_from_value(self._values[1], width)

        style = self.get_style_context()
        foreground = style.get_color()
        success, accent = style.lookup_color("accent_bg_color")

        cr.set_line_width(4)
        cr.set_line_cap(1)

        # The whole track, dimmed...
        cr.set_source_rgba(
            foreground.red,
            foreground.green,
            foreground.blue,
            0.25,
        )
        cr.move_to(start_x, center_y)
        cr.line_to(end_x, center_y)
        cr.stroke()

        # ...and the part that will play, highlighted.
        if success:
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        else:
            cr.set_source_rgba(
                foreground.red,
                foreground.green,
                foreground.blue,
                1,
            )

        cr.move_to(low_x, center_y)
        cr.line_to(high_x, center_y)
        cr.stroke()

        for x in (low_x, high_x):
            cr.set_source_rgb(1, 1, 1)
            cr.arc(x, center_y, self.HANDLE_RADIUS, 0, _TAU)
            cr.fill_preserve()

            # A thin outline, so a white handle still shows on a light theme.
            cr.set_source_rgba(
                foreground.red,
                foreground.green,
                foreground.blue,
                0.35,
            )
            cr.set_line_width(1)
            cr.stroke()


class TimeRangeSelector(Gtk.Box):
    """Start / end time fields above a two-handle slider."""

    MIN_LENGTH = 0.5
    # Within this of the very start / end of the track counts as "not set".
    DEFAULT_TOLERANCE = 0.05

    def __init__(self, duration, start=None, end=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.duration = max(0.0, float(duration or 0))
        self._with_hours = self.duration >= 3600
        self._callbacks = []

        self.slider = RangeSlider()
        self.slider.set_duration(self.duration)
        self.slider.set_range(
            0.0 if start is None else float(start),
            self.duration if end is None else float(end),
        )
        self.slider.connect_changed(self._on_slider_changed)

        self.start_entry = self._create_entry(
            "start",
            "Where the track starts playing\n"
            "(e.g. 1:05, 1:05.250 or 65.25)",
            0.0,
        )
        self.end_entry = self._create_entry(
            "end",
            "Where the track stops playing\n"
            "(e.g. 3:20, 3:20.500 or 200.5)",
            1.0,
        )

        self.length_label = Gtk.Label()
        self.length_label.set_hexpand(True)
        self.length_label.set_valign(Gtk.Align.END)
        self.length_label.add_css_class("dim-label")
        self.length_label.add_css_class("caption")

        start_box = self._labelled("Start", self.start_entry, 0.0)
        start_box.set_halign(Gtk.Align.START)
        end_box = self._labelled("End", self.end_entry, 1.0)
        end_box.set_halign(Gtk.Align.END)

        fields = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        fields.append(start_box)
        fields.append(self.length_label)
        fields.append(end_box)

        # The fields come first (on top), the slider at the very bottom.
        self.append(fields)
        self.append(self.slider)

        self._sync()

    # -- public ----------------------------------------------------------

    def connect_changed(self, callback):
        """``callback()`` runs whenever the start or end changes."""
        self._callbacks.append(callback)

    def get_range(self):
        return self.slider.get_range()

    def is_valid(self):
        low, high = self.slider.get_range()
        return high - low >= min(self.MIN_LENGTH, self.duration)

    def stored_range(self):
        """``(start, end)`` as they are kept in the database: ``None`` for
        an end that is still the very start / end of the track."""
        low, high = self.slider.get_range()

        return (
            round(low, 3) if low > self.DEFAULT_TOLERANCE else None,
            round(high, 3)
            if high < self.duration - self.DEFAULT_TOLERANCE
            else None,
        )

    # -- building --------------------------------------------------------

    def _create_entry(self, which, tooltip, alignment):
        entry = Gtk.Entry()
        entry.set_width_chars(11)
        entry.set_alignment(alignment)
        entry.set_tooltip_text(tooltip)
        entry.add_css_class("monospace")
        entry.connect("activate", lambda _entry: self._commit(which))

        # Leaving the field (Tab, a click elsewhere...) applies what was typed.
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _controller: self._commit(which))
        entry.add_controller(focus)

        return entry

    @staticmethod
    def _labelled(text, entry, xalign):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        caption = Gtk.Label(label=text, xalign=xalign)
        caption.add_css_class("caption-heading")
        box.append(caption)
        box.append(entry)
        return box

    # -- keeping fields and slider in step -------------------------------

    def _format(self, seconds):
        return format_time(seconds, self._with_hours)

    def _sync(self):
        low, high = self.slider.get_range()
        self.start_entry.set_text(self._format(low))
        self.end_entry.set_text(self._format(high))
        self.length_label.set_text(f"Length {self._format(high - low)}")

    def _notify(self):
        for callback in self._callbacks:
            callback()

    def _on_slider_changed(self, low, high):
        self._sync()
        self._notify()

    def _commit(self, which):
        entry = self.start_entry if which == "start" else self.end_entry
        low, high = self.slider.get_range()
        shown = self._format(low if which == "start" else high)
        text = entry.get_text()

        # Untouched: leave the exact value alone instead of rounding it.
        if text.strip() == shown:
            return

        value = parse_time(text)
        changed = False

        if value is not None:
            if which == "start":
                self.slider.set_range(value, high)
            else:
                self.slider.set_range(low, value)

            changed = self.slider.get_range() != (low, high)

        # Whatever was typed, show the value that was actually kept
        # (reordered or clamped if need be, or the old one if it was invalid).
        self._sync()

        if changed:
            self._notify()
