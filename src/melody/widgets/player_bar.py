
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk


_PREVIEW_CSS = """
.player-preview {
    padding: 2px 6px;
    border-radius: 8px;
}
.player-preview.has-track:hover {
    background-color: alpha(currentColor, 0.07);
}
.player-preview.has-track.pressed {
    background-color: alpha(currentColor, 0.12);
}
"""
_preview_css_installed = False


def _install_preview_css():
    global _preview_css_installed

    if _preview_css_installed:
        return

    display = Gdk.Display.get_default()

    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(_PREVIEW_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _preview_css_installed = True


class SeekBar(Gtk.DrawingArea):
    def __init__(self, playback_manager):
        super().__init__()

        self.playback_manager = playback_manager
        self.duration = 0.0
        self.position = 0.0
        self.dragging = False

        self.set_hexpand(True)
        self.set_content_height(24)
        self.set_size_request(80, 24)
        self.set_draw_func(self._draw)

        self.click = Gtk.GestureClick.new()
        self.click.set_button(Gdk.BUTTON_PRIMARY)
        self.click.connect("pressed", self._on_pressed)
        self.click.connect("released", self._on_released)
        self.add_controller(self.click)

        self.drag = Gtk.GestureDrag.new()
        self.drag.set_button(Gdk.BUTTON_PRIMARY)
        self.drag.set_exclusive(False)
        self.drag.connect("drag-update", self._on_drag_update)
        self.add_controller(self.drag)

    def set_duration(self, duration):
        self.duration = max(0.0, float(duration))
        self.queue_draw()

    def set_value(self, position):
        if not self.dragging:
            self.position = max(0.0, min(float(position), self.duration))
            self.queue_draw()

    def _position_from_x(self, x):
        width = self.get_width()
        start_x = 14
        end_x = max(start_x + 1, width - 14)

        fraction = max(0.0, min(1.0, (x - start_x) / (end_x - start_x)))
        return fraction * self.duration

    def _on_pressed(self, gesture, n_press, x, y):
        if self.duration <= 0 or not self.get_sensitive():
            return

        self.dragging = True
        self.position = self._position_from_x(x)
        self.queue_draw()

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if not self.dragging:
            return

        success, start_x, _ = gesture.get_start_point()
        if not success:
            return

        self.position = self._position_from_x(start_x + offset_x)
        self.queue_draw()

    def _on_released(self, gesture, n_press, x, y):
        if not self.dragging:
            return

        self.position = self._position_from_x(x)
        self.dragging = False
        self.playback_manager.seek_clip(self.position)
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        start_x = 14
        end_x = max(start_x + 1, width - 14)
        center_y = height / 2
        progress = (
            self.position / self.duration
            if self.duration > 0
            else 0.0
        )
        progress_x = start_x + (end_x - start_x) * progress

        style = self.get_style_context()
        foreground = style.get_color()
        success, accent = style.lookup_color("accent_bg_color")

        cr.set_line_width(4)
        cr.set_line_cap(1)

        cr.set_source_rgba(
            foreground.red,
            foreground.green,
            foreground.blue,
            0.25,
        )
        cr.move_to(start_x, center_y)
        cr.line_to(end_x, center_y)
        cr.stroke()

        if success:
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        else:
            cr.set_source_rgba(
                foreground.red,
                foreground.green,
                foreground.blue,
                1,
            )

        cr.move_to(start_x, center_y)
        cr.line_to(progress_x, center_y)
        cr.stroke()

        cr.set_source_rgb(1, 1, 1)
        cr.arc(
            progress_x,
            center_y,
            10,
            0,
            6.283185307179586,
        )
        cr.fill()


class PlayerBar(Gtk.Box):
    COVER_SIZE = 44
    NOTE_SIZE = 31

    def __init__(self, playback_manager):
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=16,
            css_classes=["player-bar"],
        )

        self.playback_manager = playback_manager
        self._previous_volume = playback_manager.volume or 0.5

        # Called by the play button when nothing is loaded in the player. It
        # returns True if it started something (the selected track), False to
        # let the button do what it normally does.
        self.on_play_idle = None

        self.set_margin_start(18)
        self.set_margin_end(18)
        self.set_margin_top(10)
        self.set_margin_bottom(10)

        controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
        )

        self.previous_button = self._create_button(
            "media-skip-backward-symbolic",
            self.playback_manager.previous,
        )
        self.play_button = self._create_button(
            "media-playback-start-symbolic",
            self.press_play,
        )
        self.next_button = self._create_button(
            "media-skip-forward-symbolic",
            self.playback_manager.next,
        )

        controls.append(self.previous_button)
        controls.append(self.play_button)
        controls.append(self.next_button)
        self.append(controls)

        self.artwork = Gtk.Image.new_from_icon_name(
            "audio-x-generic-symbolic"
        )
        # The artwork's area stays 48 px; the music note shown while there is
        # no cover is smaller than a cover (about 70%).
        self.artwork.set_pixel_size(self.NOTE_SIZE)
        self.artwork.set_size_request(48, 48)

        # The cover and the title and artist: right-clicking them opens the
        # track's menu (the window attaches it to ``preview``), and double
        # clicking them calls ``on_view`` (what "View" in that menu does).
        self.on_view = None

        _install_preview_css()
        self.preview = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=16,
        )
        self.preview.add_css_class("player-preview")
        self.preview.append(self.artwork)

        track_info = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        track_info.set_size_request(150, -1)
        track_info.set_valign(Gtk.Align.CENTER)

        self.title_label = Gtk.Label(label="Nothing playing", xalign=0)
        self.title_label.set_ellipsize(3)
        self.title_label.add_css_class("heading")

        self.artist_label = Gtk.Label(label="", xalign=0)
        self.artist_label.set_ellipsize(3)
        self.artist_label.add_css_class("dim-label")

        track_info.append(self.title_label)
        track_info.append(self.artist_label)
        self.preview.append(track_info)
        self.append(self.preview)

        preview_click = Gtk.GestureClick.new()
        preview_click.set_button(Gdk.BUTTON_PRIMARY)
        preview_click.connect("pressed", self._on_preview_pressed)
        preview_click.connect("released", self._on_preview_released)
        preview_click.connect("stopped", self._on_preview_released)
        self.preview.add_controller(preview_click)

        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        progress_box.set_hexpand(True)
        progress_box.set_valign(Gtk.Align.CENTER)

        progress_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        self.current_time_label = Gtk.Label(label="0:00")
        self.current_time_label.add_css_class("dim-label")

        self.progress = SeekBar(self.playback_manager)

        self.duration_label = Gtk.Label(label="0:00")
        self.duration_label.add_css_class("dim-label")

        progress_row.append(self.current_time_label)
        progress_row.append(self.progress)
        progress_row.append(self.duration_label)
        progress_box.append(progress_row)
        self.append(progress_box)

        playback_options = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=2,
        )
        playback_options.set_valign(Gtk.Align.CENTER)

        self.repeat_button = self._create_button(
            "media-playlist-repeat-symbolic",
            self._on_repeat_clicked,
        )
        self.shuffle_button = self._create_button(
            "media-playlist-no-shuffle-symbolic",
            self._on_shuffle_clicked,
        )

        playback_options.append(self.repeat_button)
        playback_options.append(self.shuffle_button)
        self.append(playback_options)

        volume_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        volume_box.set_valign(Gtk.Align.CENTER)

        self.volume_button = Gtk.Button()
        self.volume_button.add_css_class("flat")
        self.volume_button.set_child(
            Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        )
        self.volume_button.connect("clicked", self._on_volume_button_clicked)
        volume_box.append(self.volume_button)

        self.volume = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            0,
            1,
            0.01,
        )
        self.volume.set_draw_value(False)
        self.volume.set_size_request(150, -1)
        self.volume.set_value(self.playback_manager.volume)
        self.volume.connect("value-changed", self._on_volume_changed)
        volume_box.append(self.volume)
        self.append(volume_box)

        self._update_volume_icon()
        self._update_repeat_button()
        self._update_shuffle_button()

        GLib.timeout_add(250, self._update_progress)

    @staticmethod
    def _create_button(icon_name, callback):
        button = Gtk.Button.new_from_icon_name(icon_name)
        button.add_css_class("flat")
        button.connect("clicked", lambda _button: callback())
        return button

    def press_play(self):
        """What the play button does: with nothing loaded it plays the
        selected track (if there is one), otherwise it plays or pauses."""
        if (
                self.playback_manager.current_track is None
                and self.on_play_idle is not None
                and self.on_play_idle()
        ):
            return

        self.playback_manager.toggle_playback()

    def _on_preview_pressed(self, gesture, n_press, x, y):
        # Lighter while the button is held, like a track row.
        self.preview.add_css_class("pressed")

        track = self.playback_manager.current_track

        if n_press == 2 and track is not None and self.on_view is not None:
            self.on_view(track)

    def _on_preview_released(self, *_args):
        self.preview.remove_css_class("pressed")

    def set_track(self, track):
        if track is None:
            self.preview.remove_css_class("has-track")
            self.title_label.set_text("Nothing playing")
            self.artist_label.set_text("")
            self.artwork.set_from_icon_name("audio-x-generic-symbolic")
            self.artwork.set_pixel_size(self.NOTE_SIZE)
            self.progress.set_sensitive(False)
            self.progress.set_duration(0)
            self.progress.set_value(0)
            self.current_time_label.set_text("0:00")
            self.duration_label.set_text("0:00")
            return

        self._show_track_info(track)
        self.preview.add_css_class("has-track")
        self.progress.set_sensitive(True)
        self._update_progress()

    def refresh_track(self, track):
        """The track being played was edited: show its new title, artist and
        artwork. Unlike ``set_track`` this leaves the progress alone, since the
        track keeps playing."""
        self._show_track_info(track)

    def _show_track_info(self, track):
        self.title_label.set_text(
            track.get("title") or Path(track["path"]).stem
        )
        self.artist_label.set_text(
            track.get("artist") or "Unknown Artist"
        )

        # The track's own artwork, not the album's.
        artwork_path = track.get("artwork_path")

        if artwork_path and Path(artwork_path).is_file():
            self.artwork.set_from_file(str(artwork_path))
            self.artwork.set_pixel_size(self.COVER_SIZE)
        else:
            self.artwork.set_from_icon_name("audio-x-generic-symbolic")
            self.artwork.set_pixel_size(self.NOTE_SIZE)

    def sync_options(self):
        """Show the repeat and shuffle modes the player is really in: they
        can be changed from outside the bar (by the media keys, for one)."""
        self._update_repeat_button()
        self._update_shuffle_button()

    def set_volume(self, volume):
        """Move the volume slider, which sets the player's volume."""
        self.volume.set_value(volume)

    def update_playback_state(self, state):
        icon = (
            "media-playback-pause-symbolic"
            if state == "playing"
            else "media-playback-start-symbolic"
        )
        self.play_button.set_icon_name(icon)

    def _on_repeat_clicked(self):
        self.cycle_repeat()

    def cycle_repeat(self):
        """Go to the next repeat mode (off, all, one). What the repeat button
        does, and the R key."""
        self.playback_manager.cycle_repeat_mode()
        self._update_repeat_button()

    def _update_repeat_button(self):
        mode = self.playback_manager.repeat_mode

        if mode == "one":
            icon = "media-playlist-repeat-song-symbolic"
            tooltip = "Repeat one"
        else:
            icon = "media-playlist-repeat-symbolic"
            tooltip = "Repeat all" if mode == "all" else "Repeat off"

        self.repeat_button.set_icon_name(icon)
        self.repeat_button.set_tooltip_text(tooltip)

        if mode == "off":
            self.repeat_button.add_css_class("dim-label")
        else:
            self.repeat_button.remove_css_class("dim-label")

    def _on_shuffle_clicked(self):
        self.toggle_shuffle()

    def toggle_shuffle(self):
        """Turn shuffle on or off. What the shuffle button does, and the S
        key."""
        self.playback_manager.toggle_shuffle()
        self._update_shuffle_button()

    def _update_shuffle_button(self):
        enabled = self.playback_manager.shuffle

        self.shuffle_button.set_icon_name(
            "media-playlist-shuffle-symbolic"
            if enabled
            else "media-playlist-no-shuffle-symbolic"
        )
        self.shuffle_button.set_tooltip_text(
            "Shuffle on" if enabled else "Shuffle off"
        )

        if enabled:
            self.shuffle_button.remove_css_class("dim-label")
        else:
            self.shuffle_button.add_css_class("dim-label")

    def _on_volume_changed(self, scale):
        volume = scale.get_value()
        self.playback_manager.set_volume(volume)

        if volume > 0:
            self._previous_volume = volume

        self._update_volume_icon()

    def _on_volume_button_clicked(self, button):
        self.toggle_mute()

    def toggle_mute(self):
        """Mute the player, or bring the volume back to what it was. What the
        volume button does, and the M key."""
        if self.volume.get_value() > 0:
            self._previous_volume = self.volume.get_value()
            self.volume.set_value(0)
        else:
            self.volume.set_value(self._previous_volume or 0.5)

    def _update_volume_icon(self):
        volume = self.volume.get_value()

        if volume == 0:
            icon = "audio-volume-muted-symbolic"
        elif volume < 0.33:
            icon = "audio-volume-low-symbolic"
        elif volume < 0.66:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"

        self.volume_button.set_child(Gtk.Image.new_from_icon_name(icon))

    def _update_progress(self):
        duration = self.playback_manager.get_clip_duration()
        position = self.playback_manager.get_clip_position()

        self.progress.set_duration(duration)
        self.progress.set_value(position)

        self.current_time_label.set_text(self._format_time(position))
        self.duration_label.set_text(self._format_time(duration))

        self.update_playback_state(self.playback_manager.state)

        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _format_time(seconds):
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)

        if hours:
            return f"{hours}:{minutes:02}:{seconds:02}"

        return f"{minutes}:{seconds:02}"
