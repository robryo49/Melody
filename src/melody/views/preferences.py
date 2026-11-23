import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk

from melody.services.compilations import format_clock
from melody.widgets.range_selector import parse_time


class PreferencesView(Adw.PreferencesPage):
    """The preferences. Every change is applied (and saved) right away."""

    def __init__(self, settings, on_rescan=None):
        super().__init__(title="Preferences", icon_name="preferences-system-symbolic")

        self.settings = settings
        # ``on_rescan(done)`` starts a library scan and calls ``done()`` once
        # it is over. It returns False when it could not start one.
        self._on_rescan = on_rescan

        self._create_library_group()
        self._create_organization_group()
        self._create_compilations_group()
        self._create_recent_group()

    def _create_library_group(self):
        group = Adw.PreferencesGroup(
            title="Music library",
            description="Configure where Melody stores your music.",
        )

        self.music_folder = Adw.ActionRow(
            title="Music folder",
            subtitle="~/Musique",
        )
        self.music_folder.add_suffix(
            Gtk.Button(label="Change", valign=Gtk.Align.CENTER)
        )
        group.add(self.music_folder)

        self.rescan_row = Adw.ActionRow(
            title="Rescan library",
            subtitle=(
                "Check every file again and move the ones that are not "
                "where their tags say they belong."
            ),
        )
        self.rescan_button = Gtk.Button(
            label="Rescan",
            valign=Gtk.Align.CENTER,
        )
        self.rescan_button.connect("clicked", self._on_rescan_clicked)
        self.rescan_row.add_suffix(self.rescan_button)
        self.rescan_row.set_activatable_widget(self.rescan_button)
        group.add(self.rescan_row)

        self.add(group)

    def _create_organization_group(self):
        group = Adw.PreferencesGroup(
            title="Organization",
            description="Control how your music files are organized.",
        )

        self.organize_files = Adw.SwitchRow(
            title="Automatically organize imported music",
            subtitle="Sort files into artist and album folders.",
        )
        self.organize_files.set_active(True)
        group.add(self.organize_files)

        self.add(group)

    def _create_compilations_group(self):
        group = Adw.PreferencesGroup(
            title="Compilations",
            description=(
                "Tell long audio files (mixes, live sets...) apart from "
                "single tracks."
            ),
        )

        self.compilations_row = Adw.SwitchRow(
            title="Enable compilations",
            subtitle=(
                "Compilations get their own page instead of appearing "
                "in the Library."
            ),
        )
        self.compilations_row.set_active(self.settings.compilations_enabled)
        self.compilations_row.connect(
            "notify::active",
            self._on_compilations_toggled,
        )
        group.add(self.compilations_row)

        # Only shown while compilations are enabled.
        self.threshold_row = Adw.EntryRow(
            title="Minimum length of a compilation",
        )
        self.threshold_row.set_text(
            format_clock(self.settings.compilation_threshold)
        )
        self.threshold_row.set_tooltip_text(
            "Audio longer than this is a compilation (e.g. 15:00 or 1:30:00)."
        )
        self.threshold_row.set_show_apply_button(True)
        self.threshold_row.connect("apply", self._on_threshold_applied)
        group.add(self.threshold_row)

        self.in_albums_row = Adw.SwitchRow(
            title="Show compilations in albums",
            subtitle=(
                "A compilation also appears in (and plays with) the album "
                "its file belongs to."
            ),
        )
        self.in_albums_row.set_active(
            self.settings.show_compilations_in_albums
        )
        self.in_albums_row.connect(
            "notify::active",
            self._on_in_albums_toggled,
        )
        group.add(self.in_albums_row)

        self.add(group)
        self._sync_compilation_rows()

    def _create_recent_group(self):
        group = Adw.PreferencesGroup(
            title="Recent",
            description="Choose which tracks the Recent page shows.",
        )

        self.recent_mode_row = Adw.ComboRow(title="Show")
        self.recent_mode_row.set_model(Gtk.StringList.new([
            "Tracks added in the last N days",
            "The N most recently added tracks",
        ]))
        self.recent_mode_row.set_selected(
            0 if self.settings.recent_by_days else 1
        )
        self.recent_mode_row.connect(
            "notify::selected",
            self._on_recent_mode_changed,
        )
        group.add(self.recent_mode_row)

        # Only the one that goes with the chosen mode is shown.
        self.recent_days_row = Adw.SpinRow.new_with_range(1, 3650, 1)
        self.recent_days_row.set_title("Number of days")
        self.recent_days_row.set_value(self.settings.recent_days)
        self.recent_days_row.connect(
            "notify::value",
            self._on_recent_days_changed,
        )
        group.add(self.recent_days_row)

        self.recent_count_row = Adw.SpinRow.new_with_range(1, 10000, 1)
        self.recent_count_row.set_title("Number of tracks")
        self.recent_count_row.set_value(self.settings.recent_count)
        self.recent_count_row.connect(
            "notify::value",
            self._on_recent_count_changed,
        )
        group.add(self.recent_count_row)

        self.add(group)
        self._sync_recent_rows()

    def _sync_recent_rows(self):
        by_days = self.settings.recent_by_days
        self.recent_days_row.set_visible(by_days)
        self.recent_count_row.set_visible(not by_days)

    def _on_recent_mode_changed(self, row, _pspec):
        self.settings.recent_by_days = row.get_selected() == 0
        self._sync_recent_rows()

    def _on_recent_days_changed(self, row, _pspec):
        self.settings.recent_days = row.get_value()

    def _on_recent_count_changed(self, row, _pspec):
        self.settings.recent_count = row.get_value()

    def _on_rescan_clicked(self, button):
        if self._on_rescan is None:
            return

        button.set_sensitive(False)
        button.set_label("Scanning…")

        def done():
            button.set_label("Rescan")
            button.set_sensitive(True)

        if not self._on_rescan(done):
            done()

    def _sync_compilation_rows(self):
        enabled = self.settings.compilations_enabled
        self.threshold_row.set_visible(enabled)
        self.in_albums_row.set_visible(enabled)

    def _on_compilations_toggled(self, row, _pspec):
        self.settings.compilations_enabled = row.get_active()
        self._sync_compilation_rows()

    def _on_in_albums_toggled(self, row, _pspec):
        self.settings.show_compilations_in_albums = row.get_active()

    def _on_threshold_applied(self, row):
        seconds = parse_time(row.get_text())

        if seconds is None or seconds < 1:
            # Not a time: go back to the one in use.
            row.set_text(format_clock(self.settings.compilation_threshold))
            return

        self.settings.compilation_threshold = seconds
        row.set_text(format_clock(seconds))


def show_preferences_dialog(parent, settings, on_rescan=None):
    """Open the preferences in a window of their own, over ``parent``."""
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Preferences")
    window.set_default_size(540, 600)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(PreferencesView(settings, on_rescan))
    window.set_content(toolbar)

    keys = Gtk.EventControllerKey()

    def on_key_pressed(_controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False

    keys.connect("key-pressed", on_key_pressed)
    window.add_controller(keys)

    window.present()
