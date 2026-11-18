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


# ----------------------------------------------------------------------
# Playback helpers shared by the track context menus
# ----------------------------------------------------------------------

def is_current_track(playback_manager, track):
    current = playback_manager.current_track if playback_manager else None
    return current is not None and current.get("path") == track.get("path")


def track_is_playing(playback_manager, track):
    """True when ``track`` is the current track and is actually playing."""
    return (
        playback_manager is not None
        and playback_manager.state == "playing"
        and is_current_track(playback_manager, track)
    )


def toggle_track_playback(playback_manager, track, queue):
    """Pause/resume ``track`` if it is current, otherwise play it from ``queue``."""
    if playback_manager is None:
        return

    if is_current_track(playback_manager, track):
        playback_manager.toggle_playback()
        return

    for index, candidate in enumerate(queue):
        if candidate.get("path") == track.get("path"):
            playback_manager.play_queue(queue, index)
            return

    playback_manager.play_track(track)


# ----------------------------------------------------------------------
# Track dialogs
# ----------------------------------------------------------------------

def _close_on_escape(window):
    controller = Gtk.EventControllerKey()

    def on_key_pressed(_controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False

    controller.connect("key-pressed", on_key_pressed)
    window.add_controller(controller)


def show_edit_track_dialog(parent, track, callback):
    """Open a window to edit a track's title (file name), artist and album.

    ``callback`` receives ``{"title": ..., "artist": ..., "album": ...}`` and
    is only called when something actually changed.
    """
    # To add more editable fields later, add them to this tuple (and handle
    # them in the callback / LibraryEditor).
    fields = (
        ("title", "Title"),
        ("artist", "Artist"),
        ("album", "Album"),
    )
    original = {key: str(track.get(key) or "") for key, _label in fields}

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Edit track")
    window.set_default_size(440, -1)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    save_button = Gtk.Button(label="Save")
    save_button.add_css_class("suggested-action")
    save_button.set_sensitive(False)
    header.pack_start(cancel_button)
    header.pack_end(save_button)
    toolbar.add_top_bar(header)

    group = Adw.PreferencesGroup()
    group.set_description(
        "The file is renamed and moved to match its title, artist and album."
    )
    group.set_margin_top(12)
    group.set_margin_bottom(24)
    group.set_margin_start(24)
    group.set_margin_end(24)

    entries = {}
    for key, label in fields:
        entry_row = Adw.EntryRow()
        entry_row.set_title(label)
        entry_row.set_text(original[key])
        group.add(entry_row)
        entries[key] = entry_row

    def current_values():
        return {key: row.get_text().strip() for key, row in entries.items()}

    def can_save():
        values = current_values()
        return all(values.values()) and values != original

    def on_changed(*_args):
        save_button.set_sensitive(can_save())

    def on_save(*_args):
        if not can_save():
            return
        values = current_values()
        window.close()
        callback(values)

    for entry_row in entries.values():
        entry_row.connect("changed", on_changed)
        entry_row.connect("entry-activated", on_save)

    cancel_button.connect("clicked", lambda _button: window.close())
    save_button.connect("clicked", on_save)
    _close_on_escape(window)

    toolbar.set_content(group)
    window.set_content(toolbar)
    window.present()
    entries["title"].grab_focus()


def show_playlist_picker_dialog(
        parent,
        track,
        playlists,
        on_select,
        on_create=None,
):
    """Open a window listing playlists to add ``track`` to.

    ``on_select(playlist_id)`` is called when a playlist is picked and
    ``on_create()`` when "New playlist…" is chosen (if provided).
    """
    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Add to playlist")
    window.set_default_size(420, -1)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(12)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    track_title = str(track.get("title") or "Unknown Title")
    prompt = Gtk.Label(
        label=f"Choose a playlist for “{track_title}”",
        xalign=0,
        wrap=True,
    )
    prompt.add_css_class("dim-label")
    content.append(prompt)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.set_activate_on_single_click(True)
    listbox.add_css_class("boxed-list")

    def add_row(title, subtitle, icon_name, playlist_id):
        row = Adw.ActionRow()
        row.set_use_markup(False)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
        row.set_activatable(True)
        row.playlist_id = playlist_id
        listbox.append(row)

    if on_create is not None:
        add_row("New playlist…", None, "list-add-symbolic", None)

    for playlist in playlists:
        count = playlist.get("track_count", 0)
        add_row(
            playlist["name"],
            f"{count} track{'s' if count != 1 else ''}",
            "view-list-symbolic",
            playlist["id"],
        )

    if not playlists:
        empty = Gtk.Label(label="You don't have any playlists yet.", xalign=0)
        empty.add_css_class("dim-label")
        content.append(empty)

    def on_row_activated(_listbox, row):
        window.close()
        if row.playlist_id is None:
            on_create()
        else:
            on_select(row.playlist_id)

    listbox.connect("row-activated", on_row_activated)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_propagate_natural_height(True)
    scrolled.set_max_content_height(360)
    scrolled.set_child(listbox)
    content.append(scrolled)

    _close_on_escape(window)

    toolbar.set_content(content)
    window.set_content(toolbar)
    window.present()


# ----------------------------------------------------------------------
# Track context menu
# ----------------------------------------------------------------------

def attach_track_context_menu(
        row,
        track,
        *,
        on_play_pause,
        is_playing,
        on_edit,
        on_add_to_playlist,
        on_remove_from_playlist=None,
):
    """Right-click menu for a track row.

    Every action is a flat menu entry; anything needing more input (editing,
    choosing a playlist) opens its own window instead of a sub-menu.
    All callbacks receive the track. ``is_playing(track)`` decides whether
    the first entry reads "Play" or "Pause".
    """
    gesture = Gtk.GestureClick.new()
    gesture.set_button(3)

    def on_pressed(gesture, press_count, x, y):
        popover = Gtk.Popover()
        popover.set_parent(row)
        # Like a native context menu: no arrow, with the menu's top-left
        # corner at the pointer.
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        popover.set_position(Gtk.PositionType.BOTTOM)

        menu = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        menu.set_margin_top(6)
        menu.set_margin_bottom(6)
        menu.set_margin_start(6)
        menu.set_margin_end(6)

        def add_item(label, callback):
            button = Gtk.Button()
            button.set_has_frame(False)
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            button.set_child(Gtk.Label(label=label, xalign=0))

            def activate(_button):
                popover.popdown()
                callback(track)

            button.connect("clicked", activate)
            menu.append(button)

        add_item("Pause" if is_playing(track) else "Play", on_play_pause)
        add_item("Edit…", on_edit)
        add_item("Add to playlist…", on_add_to_playlist)

        if on_remove_from_playlist is not None:
            menu.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            add_item("Remove from playlist", on_remove_from_playlist)

        # (x, y) are relative to ``row``, which is what the popover is
        # parented to, so point at them directly.
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        popover.set_child(menu)
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda _popover: popover.unparent())
        popover.popup()

    gesture.connect("pressed", on_pressed)
    row.add_controller(gesture)
