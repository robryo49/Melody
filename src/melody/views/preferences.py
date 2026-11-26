import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from melody.services.compilations import format_clock
from melody.services.import_formats import IMPORT_FORMATS, import_format
from melody.widgets.range_selector import parse_time


class PreferencesView(Adw.PreferencesPage):
    """The preferences. Every change is applied (and saved) right away."""

    def __init__(self, settings, on_rescan=None, on_change_folder=None):
        super().__init__(title="Preferences", icon_name="preferences-system-symbolic")

        self.settings = settings
        # ``on_rescan(done)`` starts a library scan and calls ``done()`` once
        # it is over. It returns False when it could not start one.
        self._on_rescan = on_rescan
        # ``on_change_folder(folder, done)`` makes ``folder`` the music folder
        # and scans it, calling ``done()`` once that is over. It returns
        # False when it could not (a scan is already running).
        self._on_change_folder = on_change_folder

        self._create_library_group()
        self._create_organization_group()
        self._create_import_group()
        self._create_compilations_group()
        self._create_playback_group()

    def _create_playback_group(self):
        group = Adw.PreferencesGroup(
            title="Playback",
            description="How one track follows the next.",
        )

        self.gapless_row = Adw.SwitchRow(
            title="Gapless playback",
            subtitle=(
                "No pause between two tracks, for albums meant to run "
                "together. Not used while crossfade is on."
            ),
        )
        self.gapless_row.set_active(self.settings.gapless_enabled)
        self.gapless_row.connect(
            "notify::active",
            self._on_gapless_toggled,
        )
        group.add(self.gapless_row)

        self.crossfade_row = Adw.SwitchRow(
            title="Crossfade",
            subtitle="The end of a track fades into the start of the next one.",
        )
        self.crossfade_row.set_active(self.settings.crossfade_enabled)
        self.crossfade_row.connect(
            "notify::active",
            self._on_crossfade_toggled,
        )
        group.add(self.crossfade_row)

        self.crossfade_seconds_row = Adw.SpinRow.new_with_range(1, 12, 1)
        self.crossfade_seconds_row.set_title("Crossfade duration")
        self.crossfade_seconds_row.set_subtitle("In seconds.")
        self.crossfade_seconds_row.set_digits(0)
        self.crossfade_seconds_row.set_value(self.settings.crossfade_seconds)
        self.crossfade_seconds_row.connect(
            "notify::value",
            self._on_crossfade_seconds_changed,
        )
        group.add(self.crossfade_seconds_row)

        self.add(group)
        self._sync_playback_rows()

    def _sync_playback_rows(self):
        crossfade = self.settings.crossfade_enabled
        self.gapless_row.set_sensitive(not crossfade)
        self.crossfade_seconds_row.set_sensitive(crossfade)

    def _on_gapless_toggled(self, row, _pspec):
        self.settings.gapless_enabled = row.get_active()

    def _on_crossfade_toggled(self, row, _pspec):
        self.settings.crossfade_enabled = row.get_active()
        self._sync_playback_rows()

    def _on_crossfade_seconds_changed(self, row, _pspec):
        self.settings.crossfade_seconds = row.get_value()

    def _create_library_group(self):
        group = Adw.PreferencesGroup(
            title="Music library",
            description="Configure where Melody stores your music.",
        )

        self.music_folder = Adw.ActionRow(
            title="Music folder",
            subtitle=str(self.settings.music_folder),
        )
        # So the path can be copied.
        self.music_folder.set_subtitle_selectable(True)
        self.change_folder_button = Gtk.Button(
            label="Change",
            valign=Gtk.Align.CENTER,
        )
        self.change_folder_button.connect(
            "clicked",
            self._on_change_folder_clicked,
        )
        self.music_folder.add_suffix(self.change_folder_button)
        group.add(self.music_folder)

        self.rescan_row = Adw.ActionRow(
            title="Rescan library",
            subtitle=self._rescan_subtitle(),
        )
        self.rescan_button = Gtk.Button(
            label="Rescan",
            valign=Gtk.Align.CENTER,
        )
        self.rescan_button.connect("clicked", self._on_rescan_clicked)
        self.rescan_row.add_suffix(self.rescan_button)
        self.rescan_row.set_activatable_widget(self.rescan_button)
        group.add(self.rescan_row)

        self.auto_scan_row = Adw.SwitchRow(
            title="Watch the music folder",
            subtitle=(
                "Scan again by itself when music is added to, changed in "
                "or removed from the folder by hand."
            ),
        )
        self.auto_scan_row.set_active(self.settings.auto_scan_enabled)
        self.auto_scan_row.connect(
            "notify::active",
            self._on_auto_scan_toggled,
        )
        group.add(self.auto_scan_row)

        self.add(group)

    def _create_organization_group(self):
        group = Adw.PreferencesGroup(
            title="Organization",
            description="Control how your music files are organized.",
        )

        self.organize_files = Adw.SwitchRow(
            title="Organize music by artist and album",
            subtitle=(
                "When the library is scanned, move files into Artist/Album "
                "folders according to their tags. Music imported or edited "
                "in Melody is always filed this way."
            ),
        )
        self.organize_files.set_active(self.settings.organize_files)
        self.organize_files.connect(
            "notify::active",
            self._on_organize_toggled,
        )
        group.add(self.organize_files)

        self.add(group)

    def _create_import_group(self):
        group = Adw.PreferencesGroup(
            title="Import",
            description="Control how music you import is saved.",
        )

        self._import_format_keys = list(IMPORT_FORMATS)

        self.import_format_row = Adw.ComboRow(
            title="Format of downloads",
        )
        self.import_format_row.set_model(Gtk.StringList.new([
            IMPORT_FORMATS[key]["label"] for key in self._import_format_keys
        ]))
        self.import_format_row.set_selected(
            self._import_format_keys.index(
                import_format(self.settings.import_format)["key"]
            )
        )
        self._sync_import_format_subtitle()
        self.import_format_row.connect(
            "notify::selected",
            self._on_import_format_changed,
        )
        group.add(self.import_format_row)

        self.add(group)

    def _sync_import_format_subtitle(self):
        chosen = import_format(self.settings.import_format)
        self.import_format_row.set_subtitle(
            f"Audio downloaded from YouTube is converted to this format "
            f"(this needs ffmpeg). {chosen['note']}"
        )

    def _on_import_format_changed(self, row, _pspec):
        self.settings.import_format = self._import_format_keys[
            row.get_selected()
        ]
        self._sync_import_format_subtitle()

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

    def _on_auto_scan_toggled(self, row, _pspec):
        self.settings.auto_scan_enabled = row.get_active()

    def _rescan_subtitle(self):
        if self.settings.organize_files:
            return (
                "Check every file again and move the ones that are not "
                "where their tags say they belong."
            )

        return "Check every file again and bring the library up to date."

    def _on_organize_toggled(self, row, _pspec):
        self.settings.organize_files = row.get_active()
        self.rescan_row.set_subtitle(self._rescan_subtitle())

    # ------------------------------------------------------------------
    # Changing the music folder
    # ------------------------------------------------------------------

    def _message(self, heading, body):
        dialog = Adw.MessageDialog.new(self.get_root(), heading, body)
        dialog.add_response("ok", "OK")
        dialog.set_close_response("ok")
        dialog.present()

    def _on_change_folder_clicked(self, button):
        if self._on_change_folder is None:
            return

        dialog = Gtk.FileDialog()
        dialog.set_title("Choose the music folder")

        current = self.settings.music_folder

        if current.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            chosen = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # dismissed

        location = chosen.get_path()

        if not location:
            return

        folder = Path(location).resolve()

        try:
            same = folder == self.settings.music_folder.expanduser().resolve()
        except OSError:
            same = False

        if same:
            return

        # Melody scans the folder, and files it by tags: pointing it at a
        # folder that holds everything would shuffle the person's files.
        if folder in (Path.home().resolve(), Path(folder.anchor)):
            self._message(
                "Choose a folder for your music",
                "This folder holds far more than music. Pick (or create) a "
                "folder meant for your music.",
            )
            return

        if not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
            self._message(
                "This folder cannot be used",
                "Melody needs to read and write in its music folder.",
            )
            return

        self._confirm_folder(folder)

    def _confirm_folder(self, folder):
        body = (
            f"Melody will scan {folder} and make it your library.\n\n"
            "Music that is not in the new folder leaves your library, with "
            "its play count, its favorite mark and its place in playlists. "
            "The files themselves are not touched. If you moved or copied "
            "your music there as it is, nothing is lost."
        )

        if self.settings.organize_files:
            body += (
                "\n\nFiles in the new folder will be moved into "
                "Artist/Album folders according to their tags (see "
                "\u201cOrganize music by artist and album\u201d below)."
            )

        dialog = Adw.MessageDialog.new(
            self.get_root(),
            f"Use \u201c{folder.name}\u201d as the music folder?",
            body,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("change", "Change folder")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _dialog, response: (
                self._apply_folder(folder) if response == "change" else None
            ),
        )
        dialog.present()

    def _apply_folder(self, folder):
        self.change_folder_button.set_sensitive(False)

        def done():
            self.change_folder_button.set_sensitive(True)

        if not self._on_change_folder(folder, done):
            done()
            self._message(
                "The library is busy",
                "Wait for the library scan to finish, then try again.",
            )
            return

        self.music_folder.set_subtitle(str(self.settings.music_folder))

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


def show_preferences_dialog(
        parent,
        settings,
        on_rescan=None,
        on_change_folder=None,
):
    """Open the preferences in a window of their own, over ``parent``."""
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Preferences")
    # Fixed size: the preferences scroll inside it.
    window.set_resizable(False)
    window.set_size_request(540, 600)
    window.set_default_size(540, 600)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(PreferencesView(settings, on_rescan, on_change_folder))
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
