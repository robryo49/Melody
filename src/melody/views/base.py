import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk


class BasePage(Gtk.Box):
    def __init__(self, title, description, icon):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.page_title = title

        status_page = Adw.StatusPage(
            title=title,
            description=description,
            icon_name=icon,
        )
        status_page.set_vexpand(True)

        self.append(status_page)
