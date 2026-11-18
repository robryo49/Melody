import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk


SORT_OPTIONS = ["Title", "Album", "Artist", "Duration"]


def sort_tracks(tracks, selected_index):
    keys = {
        0: lambda track: str(track.get("title") or "").casefold(),
        1: lambda track: str(track.get("album") or "").casefold(),
        2: lambda track: str(track.get("artist") or "").casefold(),
        3: lambda track: float(track.get("duration") or 0),
    }
    return sorted(tracks, key=keys.get(selected_index, keys[0]))


def show_text_dialog(parent, heading, description, initial_text, callback):
    dialog = Adw.MessageDialog.new(parent, heading, description)
    entry = Gtk.Entry()
    entry.set_text(initial_text or "")
    entry.set_activates_default(True)
    entry.set_hexpand(True)
    dialog.set_extra_child(entry)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance(
        "save",
        Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def on_response(dialog, response):
        if response == "save":
            value = entry.get_text().strip()
            if value:
                callback(value)

    dialog.connect("response", on_response)
    dialog.present()


def show_merge_confirmation(parent, collection_type, name, callback):
    dialog = Adw.MessageDialog.new(
        parent,
        f"Merge this {collection_type}?",
        (
            f'"{name}" already exists. This action will merge this '
            f"{collection_type} with the existing one."
        ),
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", "Confirm")
    dialog.set_response_appearance(
        "confirm",
        Adw.ResponseAppearance.DESTRUCTIVE,
    )
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(dialog, response):
        if response == "confirm":
            callback()

    dialog.connect("response", on_response)
    dialog.present()


def show_error_dialog(parent, heading, message):
    dialog = Adw.MessageDialog.new(parent, heading, str(message))
    dialog.add_response("close", "Close")
    dialog.set_default_response("close")
    dialog.present()


def attach_track_context_menu(
        row,
        track,
        get_playlists,
        add_to_playlist,
        create_playlist=None,
        remove_from_playlist=None,
):
    gesture = Gtk.GestureClick.new()
    gesture.set_button(3)

    def on_pressed(gesture, press_count, x, y):
        playlists = get_playlists() if get_playlists else []

        popover = Gtk.Popover()
        popover.set_parent(row)
        popover.set_has_arrow(True)

        menu = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        menu.set_margin_top(6)
        menu.set_margin_bottom(6)
        menu.set_margin_start(6)
        menu.set_margin_end(6)

        if playlists:
            heading = Gtk.Label(label="Add to playlist", xalign=0)
            heading.add_css_class("heading")
            heading.set_margin_start(8)
            heading.set_margin_top(4)
            heading.set_margin_bottom(4)
            menu.append(heading)

            for playlist in playlists:
                button = Gtk.Button(label=playlist["name"])
                button.set_has_frame(False)
                button.add_css_class("flat")
                button.set_halign(Gtk.Align.FILL)
                button.connect(
                    "clicked",
                    lambda _button, playlist_id=playlist["id"]:
                    (popover.popdown(),
                     add_to_playlist(track, playlist_id)),
                )
                menu.append(button)
        else:
            empty = Gtk.Label(label="No playlists yet", xalign=0)
            empty.add_css_class("dim-label")
            empty.set_margin_start(8)
            empty.set_margin_end(8)
            menu.append(empty)

        if create_playlist is not None:
            create_button = Gtk.Button(label="Create playlist…")
            create_button.set_has_frame(False)
            create_button.add_css_class("flat")
            create_button.set_halign(Gtk.Align.FILL)
            create_button.connect(
                "clicked",
                lambda _button: (popover.popdown(), create_playlist(track)),
            )
            menu.append(create_button)

        if remove_from_playlist is not None:
            if playlists:
                menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            remove_button = Gtk.Button(label="Remove from playlist")
            remove_button.set_has_frame(False)
            remove_button.add_css_class("flat")
            remove_button.set_halign(Gtk.Align.FILL)
            remove_button.connect(
                "clicked",
                lambda _button: (popover.popdown(), remove_from_playlist(track)),
            )
            menu.append(remove_button)

        popover.set_child(menu)
        popover.set_pointing_to(
            Gdk.Rectangle(
                x=int(x),
                y=int(y),
                width=1,
                height=1,
            )
        )
        popover.connect("closed", lambda _popover: popover.unparent())
        popover.popup()

    gesture.connect("pressed", on_pressed)
    row.add_controller(gesture)