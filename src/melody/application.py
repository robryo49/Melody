import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio

from melody.window import MelodyWindow


class MelodyApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.robin.Melody",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        window = self.props.active_window

        if window is None:
            window = MelodyWindow(application=self)

        window.present()
