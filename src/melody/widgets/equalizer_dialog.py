"""The equalizer, and the ReplayGain switch that goes with it.

``EqualizerControls`` holds the on and off switch, the presets, the reset
button and one slider per band. ``ReplayGainControls`` holds the ReplayGain
switch and its "Analyse…" button. Both show and change the same settings
wherever they are shown: the equalizer window (``show_equalizer_dialog``) and
the Playback tab of the preferences use them. Every change is heard at once,
and the controls follow the equalizer when it is changed from elsewhere.

The equalizer window is modal, like the other windows: the main window is
locked (and dimmed by the desktop) while it is open, and the window stays
attached to it.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from melody.services.equalizer import (
    BAND_COUNT,
    BAND_LABELS,
    CUSTOM,
    GAIN_STEP,
    MAX_GAIN,
    MIN_GAIN,
    PRESETS,
)

SLIDER_HEIGHT = 190

# The window showing now, so a second click on "Equalizer" brings it back
# instead of opening another one.
_open_window = None


def _format_gain(gain):
    return "0" if gain == 0 else f"{gain:+g}"


class EqualizerControls(Gtk.Box):
    """The equalizer's switch, presets and sliders.

    ``available`` is False when the player has no equalizer (GStreamer's
    plugin is missing): a note takes the place of the controls. Call
    ``release()`` when the controls are no longer shown, so they stop
    following the equalizer.
    """

    def __init__(self, equalizer, available=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.equalizer = equalizer
        self.available = available
        # True while the controls are being set to match the equalizer, so
        # that is not taken for the person moving them.
        self._syncing = False

        if available:
            self.append(self._build_top_row())
            self.append(self._build_sliders())
        else:
            self.append(self._build_unavailable_note())

        equalizer.connect_changed(self._on_equalizer_changed)
        self._sync()

    def release(self):
        self.equalizer.disconnect_changed(self._on_equalizer_changed)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_unavailable_note(self):
        note = Gtk.Label(
            label="GStreamer's equalizer plugin was not found, so there is "
                  "no equalizer. It is part of gst-plugins-good: install "
                  "that package, then restart Melody.",
            xalign=0,
        )
        note.set_wrap(True)
        note.add_css_class("dim-label")
        return note

    def _build_top_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.connect("notify::active", self._on_enabled_changed)

        switch_label = Gtk.Label(label="Equalizer", xalign=0)
        switch_label.add_css_class("heading")

        self.preset_names = list(PRESETS) + [CUSTOM]
        self.preset_dropdown = Gtk.DropDown.new_from_strings(self.preset_names)
        self.preset_dropdown.set_tooltip_text("Preset")
        self.preset_dropdown.connect(
            "notify::selected",
            self._on_preset_selected,
        )

        self.reset_button = Gtk.Button(label="Reset")
        self.reset_button.set_tooltip_text("Set every band back to 0 dB")
        self.reset_button.connect("clicked", self._on_reset_clicked)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)

        row.append(self.switch)
        row.append(switch_label)
        row.append(spacer)
        row.append(self.preset_dropdown)
        row.append(self.reset_button)
        return row

    def _build_sliders(self):
        self.sliders_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self.sliders_box.set_homogeneous(True)

        self.scales = []
        self.value_labels = []

        for index in range(BAND_COUNT):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

            value_label = Gtk.Label(label="0")
            value_label.add_css_class("dim-label")
            value_label.add_css_class("caption")

            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.VERTICAL,
                MIN_GAIN,
                MAX_GAIN,
                GAIN_STEP,
            )
            # Louder at the top.
            scale.set_inverted(True)
            scale.set_draw_value(False)
            scale.set_size_request(-1, SLIDER_HEIGHT)
            scale.set_vexpand(True)
            scale.set_halign(Gtk.Align.CENTER)
            scale.add_mark(0.0, Gtk.PositionType.LEFT, None)
            scale.set_tooltip_text(f"{BAND_LABELS[index]} Hz, in dB")
            scale.connect("value-changed", self._on_scale_changed, index)

            frequency = Gtk.Label(label=BAND_LABELS[index])
            frequency.add_css_class("caption")

            column.append(value_label)
            column.append(scale)
            column.append(frequency)
            self.sliders_box.append(column)

            self.scales.append(scale)
            self.value_labels.append(value_label)

        unit = Gtk.Label(
            label="Gain in dB, frequency in Hz. Boosting lowers the overall "
                  "volume a little so the sound does not distort.",
            xalign=0,
        )
        unit.set_wrap(True)
        unit.add_css_class("dim-label")
        unit.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.sliders_box)
        box.append(unit)
        return box

    # ------------------------------------------------------------------
    # The controls follow the equalizer
    # ------------------------------------------------------------------

    def _on_equalizer_changed(self):
        self._sync()

    def _sync(self):
        """Set every control to what the equalizer holds."""
        if not self.available:
            return

        self._syncing = True

        try:
            self.switch.set_active(self.equalizer.enabled)

            for scale, label, gain in zip(
                    self.scales, self.value_labels, self.equalizer.gains,
            ):
                scale.set_value(gain)
                label.set_text(_format_gain(gain))

            self._sync_preset()

            # Nothing to move while the equalizer is off.
            self.sliders_box.set_sensitive(self.equalizer.enabled)
            self.preset_dropdown.set_sensitive(self.equalizer.enabled)
            self.reset_button.set_sensitive(self.equalizer.enabled)
        finally:
            self._syncing = False

    def _sync_preset(self):
        self.preset_dropdown.set_selected(
            self.preset_names.index(self.equalizer.preset_name())
        )

    # ------------------------------------------------------------------
    # The person's changes
    # ------------------------------------------------------------------

    def _on_enabled_changed(self, switch, _pspec):
        if self._syncing:
            return

        self.equalizer.set_enabled(switch.get_active())

    def _on_scale_changed(self, scale, index):
        if self._syncing:
            return

        self.equalizer.set_gain(index, scale.get_value())

        gain = self.equalizer.gains[index]
        self.value_labels[index].set_text(_format_gain(gain))

        # Moving a slider makes it "Custom", unless it lands on a preset.
        self._syncing = True
        try:
            self._sync_preset()
        finally:
            self._syncing = False

    def _on_preset_selected(self, dropdown, _pspec):
        if self._syncing:
            return

        name = self.preset_names[dropdown.get_selected()]

        if name == CUSTOM:
            return

        self.equalizer.set_gains(PRESETS[name])

    def _on_reset_clicked(self, _button):
        self.equalizer.set_gains(PRESETS["Flat"])


class ReplayGainControls(Gtk.Box):
    """The ReplayGain switch, with a short explanation and, when ``analyse`` is
    given, the "Analyse…" button: ``analyse(parent)`` opens the analysis, with
    the window the controls are in as ``parent``.

    Call ``release()`` when the controls are no longer shown.
    """

    def __init__(self, equalizer, analyse=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self.equalizer = equalizer
        self._analyse = analyse
        self._syncing = False

        self.replaygain_switch = Gtk.Switch()
        self.replaygain_switch.set_valign(Gtk.Align.CENTER)
        self.replaygain_switch.connect(
            "notify::active",
            self._on_replaygain_changed,
        )

        title = Gtk.Label(label="ReplayGain", xalign=0)
        title.add_css_class("heading")

        description = Gtk.Label(
            label="Play tracks that have a ReplayGain tag at an even "
                  "volume, and show that tag when editing a track, where "
                  "it can be changed or set.",
            xalign=0,
        )
        description.set_wrap(True)
        description.set_max_width_chars(1)
        description.add_css_class("dim-label")
        description.add_css_class("caption")

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.set_hexpand(True)
        labels.append(title)
        labels.append(description)

        self.append(self.replaygain_switch)
        self.append(labels)

        if analyse is not None:
            analyse_button = Gtk.Button(label="Analyse…")
            analyse_button.set_valign(Gtk.Align.CENTER)
            analyse_button.set_tooltip_text(
                "Measure how loud tracks are and set their ReplayGain"
            )
            analyse_button.connect("clicked", self._on_analyse_clicked)
            self.append(analyse_button)

        equalizer.connect_changed(self._on_equalizer_changed)
        self._sync()

    def release(self):
        self.equalizer.disconnect_changed(self._on_equalizer_changed)

    def _on_analyse_clicked(self, _button):
        self._analyse(self.get_root())

    def _on_equalizer_changed(self):
        self._sync()

    def _sync(self):
        self._syncing = True

        try:
            self.replaygain_switch.set_active(self.equalizer.replaygain_enabled)
        finally:
            self._syncing = False

    def _on_replaygain_changed(self, switch, _pspec):
        if self._syncing:
            return

        self.equalizer.set_replaygain_enabled(switch.get_active())


class _EqualizerWindow(Adw.Window):
    def __init__(self, parent, equalizer, available=True, analyse=None):
        super().__init__()

        self.set_title("Equalizer")
        self.set_transient_for(parent)
        # Like the other windows, it locks the main window while it is open
        # (and the desktop keeps it attached to it, centered over it).
        self.set_modal(True)
        self.set_destroy_with_parent(True)
        # Fixed size: the width given here, the height the content needs.
        self.set_resizable(False)
        self.set_size_request(640, -1)
        self.set_default_size(640, -1)

        self.controls = EqualizerControls(equalizer, available)
        self.replaygain = ReplayGainControls(equalizer, analyse)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.set_margin_top(12)
        content.set_margin_bottom(18)
        content.append(self.controls)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        content.append(self.replaygain)

        header = Adw.HeaderBar()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, _window):
        global _open_window
        _open_window = None
        self.controls.release()
        self.replaygain.release()
        return False


def show_equalizer_dialog(parent, equalizer, available=True, analyse=None):
    """Open the equalizer window (or bring it back if it is open).

    ``available`` is False when the player has no equalizer (GStreamer's
    plugin is missing): the window then says so instead of showing sliders,
    and still has the ReplayGain switch.

    ``analyse(window)``, if given, is what the "Analyse…" button next to the
    ReplayGain switch calls, with this window as its parent.
    """
    global _open_window

    if _open_window is not None:
        _open_window.present()
        return

    _open_window = _EqualizerWindow(parent, equalizer, available, analyse)
    _open_window.present()
