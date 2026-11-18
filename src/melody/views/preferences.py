import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk


class PreferencesView(Adw.PreferencesPage):
    def __init__(self):
        super().__init__(title="Preferences", icon_name="preferences-system-symbolic")

        self._create_library_group()
        self._create_organization_group()

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
