"""Ratings: five stars, each one empty, half full or full.

Hovering over the stars fills them up to the mouse, half a star at a time, so
a rating is easy to pick. A click sets the rating, and a click on the rating
already set takes it away. The rating is kept in half stars (0 to 10), see
``melody.database``.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

STARS = 5
MAX_RATING = 2 * STARS

_EMPTY_ICON = "non-starred-symbolic"
_HALF_ICON = "semi-starred-symbolic"
_FULL_ICON = "starred-symbolic"

_CSS = """
.star-rating image {
    opacity: 0.4;
}
.star-rating image.lit {
    opacity: 1;
    color: #ffffff;
}
"""
_css_installed = False

# What a click on the stars does with the track: ``handler(track, rating)``.
# Set once by the window (see ``set_rating_handler``), so that the stars need
# no database of their own.
_rating_handler = None


def set_rating_handler(handler):
    global _rating_handler
    _rating_handler = handler


def rate_track(track, rating):
    """Give ``track`` the rating ``rating`` (in half stars)."""
    if _rating_handler is not None:
        _rating_handler(track, rating)


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


def _clamp(rating):
    return max(0, min(MAX_RATING, int(rating or 0)))


class StarRating(Gtk.Box):
    """Five stars showing a rating, which can be changed by clicking them.

    ``connect_changed(callback)`` calls ``callback(rating)`` when the person
    clicks the stars. ``set_value`` changes the rating shown without calling
    them.
    """

    def __init__(self, value=0, pixel_size=16):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        _install_css()

        self.add_css_class("star-rating")
        self.set_halign(Gtk.Align.START)
        self.set_cursor_from_name("pointer")

        self._value = _clamp(value)
        # The rating the mouse is over (while it is over the stars), or None.
        self._preview = None
        self._changed_callbacks = []
        self._images = []

        for _index in range(STARS):
            image = Gtk.Image()
            image.set_pixel_size(pixel_size)
            self.append(image)
            self._images.append(image)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

        self._refresh()

    def value(self):
        """The rating shown, in half stars."""
        return self._value

    def set_value(self, value):
        self._value = _clamp(value)
        self._preview = None
        self._refresh()

    def connect_changed(self, callback):
        self._changed_callbacks.append(callback)

    def _half_star_at(self, x):
        """The rating (in half stars) of the mouse at ``x``: the left half of a
        star is a half star, the right half a full one."""
        width = self.get_width()

        if width <= 0:
            return 0

        star_width = width / STARS
        index = min(STARS - 1, max(0, int(x // star_width)))
        inside = x - index * star_width
        return index * 2 + (1 if inside < star_width / 2 else 2)

    def _on_motion(self, _controller, x, _y):
        self._preview = self._half_star_at(x)
        self._refresh()

    def _on_leave(self, _controller):
        self._preview = None
        self._refresh()

    def _on_pressed(self, _gesture, _n_press, x, _y):
        rating = self._half_star_at(x)

        # Clicking the rating that is already set takes it away.
        if rating == self._value:
            rating = 0

        self._value = rating
        self._refresh()

        for callback in list(self._changed_callbacks):
            callback(rating)

    def _refresh(self):
        shown = self._preview if self._preview is not None else self._value

        for index, image in enumerate(self._images):
            level = shown - 2 * index

            if level >= 2:
                image.set_from_icon_name(_FULL_ICON)
            elif level == 1:
                image.set_from_icon_name(_HALF_ICON)
            else:
                image.set_from_icon_name(_EMPTY_ICON)

            if level >= 1:
                image.add_css_class("lit")
            else:
                image.remove_css_class("lit")
