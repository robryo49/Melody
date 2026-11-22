import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.collection_actions import (
    attach_track_context_menu,
    is_playback_source,
    playback_source,
    reveal_track_row,
    toggle_track_playback,
    track_is_playing,
)
from melody.services.playlist_artwork import get_playlist_cover
from melody.services.track_range import track_length
from melody.widgets.collection_grid import CollectionGrid
from melody.widgets.cover_edit_button import with_edit_button
from melody.widgets.import_dialogs import create_import_button
from melody.widgets.track_table import (
    TrackSort,
    TrackTableHeader,
    create_track_row,
    update_play_count_in_list,
)
from melody.widgets.value_label import value_label


class PlaylistsView(Gtk.Box):

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.database = None
        self.tracks = []
        self.playlists = []
        self.current_playlist = None
        self.current_tracks = []
        self.playback_manager = None
        self._playlist_paths = {}
        self._playlist_covers = {}
        # The column the playlist's track list is sorted by.
        self.track_sort = TrackSort()

        self.on_create_playlist = None
        self.on_rename_playlist = None
        self.on_add_to_playlist = None
        self.on_edit_track = None
        self.on_import = None
        self.on_change_image = None

        self._build_header()
        self._build_grid()
        self._build_details()

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.grid_page, "grid")
        self.stack.add_named(self.details_page, "details")
        self.stack.add_named(self.empty_page, "empty")

        self.append(self.stack)
        self.stack.set_visible_child_name("empty")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_data(self, database, tracks, playback_manager):
        self.database = database
        self.tracks = list(tracks)
        self.playback_manager = playback_manager

        if self.playback_manager is not None:
            GLib.timeout_add(250, self._update_play_buttons)

        self.refresh()

    def set_callbacks(
            self,
            create_playlist,
            rename_playlist,
            add_to_playlist,
            edit_track,
    ):
        self.on_create_playlist = create_playlist
        self.on_rename_playlist = rename_playlist
        self.on_add_to_playlist = add_to_playlist
        self.on_edit_track = edit_track

    def set_import_callback(self, callback):
        """``callback(context)`` imports music from a playlist's page; the
        context names the playlist, so what is imported is added to it."""
        self.on_import = callback

    def set_image_callback(self, callback):
        """``callback(playlist)`` lets the person pick a new picture for a
        playlist (the edit button over its cover)."""
        self.on_change_image = callback

    def library_tracks_of(self, playlist):
        """The playlist's tracks as the library has them, with their artwork
        (what the picture suggestions are made from)."""
        by_id = {track.get("id"): track for track in self.tracks}

        return [
            by_id[track["id"]]
            for track in self.database.get_playlist_tracks(playlist["id"])
            if track["id"] in by_id
        ]

    def view_playlist(self, playlist_id, track):
        """Show the page of the playlist with this id and scroll to ``track``
        in it. False if the playlist no longer exists."""
        if self.database is None:
            return False

        playlist = self.database.get_playlist(playlist_id)

        if not playlist:
            return False

        self._show_playlist(playlist)
        reveal_track_row(self.track_list, track)
        return True

    # ------------------------------------------------------------------
    # Header and grid
    # ------------------------------------------------------------------

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        title = Gtk.Label(label="Playlists", xalign=0)
        title.add_css_class("title-2")
        title.set_hexpand(True)

        self.count_label = Gtk.Label(label="0 playlists")
        self.count_label.add_css_class("dim-label")

        create_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        create_button.set_tooltip_text("Create playlist")
        create_button.connect("clicked", self._on_create_clicked)

        header.append(title)
        header.append(self.count_label)
        header.append(create_button)

        # Same 12px as the spacing between the header's own controls, so the
        # grid never starts flush against the bottom of the search bar.
        header.set_margin_bottom(12)
        self.grid_header = header

    def _build_grid(self):
        self.grid = CollectionGrid(
            on_open=self._show_playlist,
            placeholder_icon="view-list-symbolic",
        )

        self.empty_page = Adw.StatusPage(
            title="No playlists yet",
            description="Create playlists to organize your music.",
            icon_name="view-list-symbolic",
        )

        # With no playlist the header (and its + button) is not shown, so the
        # page itself offers the way to create the first one.
        empty_create_button = Gtk.Button(label="Create playlist")
        empty_create_button.add_css_class("pill")
        empty_create_button.add_css_class("suggested-action")
        empty_create_button.set_halign(Gtk.Align.CENTER)
        empty_create_button.connect("clicked", self._on_create_clicked)
        self.empty_page.set_child(empty_create_button)

        self.grid_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.grid_page.append(self.grid_header)
        self.grid_page.append(self.grid)

    def refresh(self):
        if self.database is None:
            return

        # A playlist is only reopened if its page is the one showing: after an
        # edit window is saved from the grid, the grid stays the grid, even
        # if a playlist was opened earlier.
        details_open = (
            self.current_playlist is not None
            and self.stack.get_visible_child_name() == "details"
        )

        self.playlists = self.database.get_playlists()
        self._refresh_grid()

        if not self.playlists:
            self.current_playlist = None
            self.current_tracks = []
            self.stack.set_visible_child_name("empty")

        elif details_open:
            refreshed = self.database.get_playlist(self.current_playlist["id"])

            if refreshed:
                self._show_playlist(refreshed)
            else:
                self.current_playlist = None
                self.current_tracks = []
                self.stack.set_visible_child_name("grid")

        else:
            self.current_playlist = None
            self.current_tracks = []
            self.stack.set_visible_child_name("grid")

        self._update_play_buttons()

    def _refresh_grid(self):
        visible_playlists = sorted(
            self.playlists,
            key=lambda playlist: str(playlist.get("name") or "").casefold(),
        )

        track_by_path = {track.get("path"): track for track in self.tracks}

        # Resolve every playlist's tracks once per refresh; the cover and the
        # "is this playlist playing" check both reuse it.
        self._playlist_paths = {}
        self._playlist_covers = {}
        for playlist in visible_playlists:
            playlist_tracks = self.database.get_playlist_tracks(playlist["id"])
            self._playlist_paths[playlist["id"]] = {
                track.get("path") for track in playlist_tracks
            }

            first = (
                        track_by_path.get(playlist_tracks[0].get("path"))
                        if playlist_tracks
                        else None
                    ) or {}
            # The picture chosen for the playlist comes first, then the cover
            # of its first track.
            self._playlist_covers[playlist["id"]] = (
                    get_playlist_cover(self.database, playlist["id"])
                    or first.get("album_artwork_path")
                    or first.get("artwork_path")
            )

        self.grid.populate(
            visible_playlists,
            describe=self._describe_playlist,
            on_play=self._toggle_playlist,
            play_tooltip="Play playlist",
            on_edit=self._on_edit_playlist_from_grid,
            open_in_menu=True,
            on_delete=self._delete_playlist,
            artwork_kind="playlist",
        )

        self.count_label.set_text(
            f"{len(self.playlists)} "
            f"playlist{'s' if len(self.playlists) != 1 else ''}"
        )

        self._update_play_buttons()

    def _on_edit_playlist_from_grid(self, playlist):
        if self.on_rename_playlist is not None:
            self.on_rename_playlist(playlist)

    def cover_for(self, playlist):
        """The cover shown for ``playlist`` in the grid (or None)."""
        return self._playlist_covers.get(playlist["id"])

    def _describe_playlist(self, playlist):
        count = playlist.get("track_count", 0)
        return (
            playlist["name"],
            f"{count} track{'s' if count != 1 else ''}",
            self._playlist_covers.get(playlist["id"]),
        )

    # ------------------------------------------------------------------
    # Playlist details
    # ------------------------------------------------------------------

    def _build_details(self):
        self.details_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_button.set_tooltip_text("Back to playlists")
        back_button.connect("clicked", self._on_back_clicked)

        self.details_heading = Gtk.Label(xalign=0)
        self.details_heading.add_css_class("title-2")
        self.details_heading.set_hexpand(True)

        header.append(back_button)
        header.append(self.details_heading)
        self.details_page.append(header)

        info = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
        )
        info.set_margin_start(24)
        info.set_margin_end(24)

        self.cover = Gtk.Picture()
        self.cover.set_size_request(200, 200)
        self.cover.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.cover.set_can_shrink(True)

        placeholder = Gtk.Image.new_from_icon_name("view-list-symbolic")
        placeholder.set_pixel_size(96)

        self.cover_stack = Gtk.Stack()
        self.cover_stack.set_size_request(200, 200)
        self.cover_stack.set_valign(Gtk.Align.START)
        self.cover_stack.add_named(self.cover, "cover")
        self.cover_stack.add_named(placeholder, "placeholder")

        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        labels.set_valign(Gtk.Align.START)
        labels.set_hexpand(True)

        self.name_label = value_label(css_classes=("title-1",))

        self.track_count_label = Gtk.Label(xalign=0)
        self.track_count_label.add_css_class("dim-label")

        self.details_play_button = Gtk.Button(label="Play")
        self.details_play_button.set_halign(Gtk.Align.START)
        self.details_play_button.connect("clicked", self._on_play_clicked)

        edit_button = Gtk.Button(label="Edit")
        edit_button.connect(
            "clicked",
            lambda _button: self._on_rename_clicked(),
        )

        delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_button.set_tooltip_text("Delete playlist")
        delete_button.add_css_class("destructive-action")
        delete_button.connect("clicked", self._on_delete_clicked)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.START)
        actions.append(self.details_play_button)
        actions.append(edit_button)
        actions.append(create_import_button(self._on_import_clicked))
        actions.append(delete_button)

        labels.append(self.name_label)
        labels.append(self.track_count_label)
        labels.append(actions)

        info.append(
            with_edit_button(self.cover_stack, self._on_change_cover_clicked)
        )
        info.append(labels)
        self.details_page.append(info)

        self.details_page.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )

        self.track_list = Gtk.ListBox()
        self.track_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.track_list.set_activate_on_single_click(False)
        self.track_list.add_css_class("boxed-list")
        self.track_list.connect("row-activated", self._on_track_activated)

        track_scroll = Gtk.ScrolledWindow()
        track_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        track_scroll.set_vexpand(True)
        track_scroll.set_margin_start(24)
        track_scroll.set_margin_end(24)
        track_scroll.set_margin_bottom(16)
        track_scroll.set_child(self.track_list)

        # The column names stay above the scrolled rows, in view while the
        # list scrolls; they sort the list when clicked.
        track_header = TrackTableHeader(
            self.track_sort,
            self._refresh_detail_tracks,
            margin_start=24,
            margin_end=24,
        )

        track_table = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        track_table.set_vexpand(True)
        track_table.append(track_header)
        track_table.append(track_scroll)

        self.details_page.append(track_table)

    def _show_playlist(self, playlist):
        self.current_playlist = playlist

        tracks = self.database.get_playlist_tracks(playlist["id"])

        by_path = {
            track.get("path"): track
            for track in self.tracks
        }

        for track in tracks:
            cached = by_path.get(track.get("path"), {})
            track["artwork_path"] = cached.get("artwork_path")
            track["album_artwork_path"] = cached.get("album_artwork_path")

        self.current_tracks = tracks

        self.details_heading.set_text("Playlist")
        self.name_label.set_text(playlist["name"])
        self.track_count_label.set_text(
            f"{len(tracks)} track{'s' if len(tracks) != 1 else ''}"
        )

        first = tracks[0] if tracks else None
        artwork_path = (
                get_playlist_cover(self.database, playlist["id"])
                or (first or {}).get("album_artwork_path")
                or (first or {}).get("artwork_path")
        )

        if artwork_path:
            self.cover.set_filename(artwork_path)
            self.cover_stack.set_visible_child_name("cover")
        else:
            self.cover.set_filename(None)
            self.cover_stack.set_visible_child_name("placeholder")

        self._refresh_detail_tracks()
        self.stack.set_visible_child_name("details")
        self._update_play_buttons()

    def _create_track_row(self, track):
        row = create_track_row(track)

        if self.on_edit_track is not None:
            playlist_id = self.current_playlist["id"]
            attach_track_context_menu(
                row,
                track,
                on_play_pause=self._toggle_track,
                is_playing=lambda t: track_is_playing(self.playback_manager, t),
                on_edit=self.on_edit_track,
                on_add_to_playlist=lambda t: self.on_add_to_playlist(
                    t, exclude_playlist_id=playlist_id
                ),
                on_remove_from_playlist=lambda t: self._remove_track(
                    playlist_id, t
                ),
            )

        return row

    def _toggle_track(self, track):
        queue = self.track_sort.sort(self.current_tracks)
        toggle_track_playback(
            self.playback_manager,
            track,
            queue,
            self._playlist_source(self.current_playlist),
        )
        self._update_play_buttons()

    def _refresh_detail_tracks(self):
        child = self.track_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.track_list.remove(child)
            child = next_child

        if self.current_playlist is None:
            return

        tracks = self.track_sort.sort(self.current_tracks)

        for track in tracks:
            self.track_list.append(self._create_track_row(track))

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _is_playlist_playing(self, playlist):
        """True when the playlist is what is being played (it was started as a
        playlist), not merely one of its tracks playing."""
        return is_playback_source(
            self.playback_manager,
            "playlist",
            playlist["id"],
        )

    @staticmethod
    def _playlist_source(playlist):
        # A playlist keeps its id when it is renamed, so the id is enough.
        return playback_source("playlist", playlist["id"])

    def _update_play_buttons(self):
        if self.playback_manager is None or self.database is None:
            return GLib.SOURCE_CONTINUE

        is_playing_state = self.playback_manager.state == "playing"
        self.grid.update_playing(
            lambda playlist: (
                    is_playing_state
                    and self._is_playlist_playing(playlist)
            )
        )

        if self.current_playlist is not None:
            playing = (
                    self._is_playlist_playing(self.current_playlist)
                    and self.playback_manager.state == "playing"
            )
            self.details_play_button.set_label("Pause" if playing else "Play")

        return GLib.SOURCE_CONTINUE

    def _toggle_playlist(self, playlist):
        if self.playback_manager is None:
            return

        if self._is_playlist_playing(playlist):
            if self.playback_manager.state == "playing":
                self.playback_manager.pause()
            else:
                self.playback_manager.play()
        else:
            self._play_playlist(playlist)

        self._update_play_buttons()

    def _playlist_queue(self, playlist):
        """The tracks of ``playlist`` in the order to play them: the order its
        page shows them in (as sorted by the selector) while it is open,
        otherwise by title.

        The tracks carry their artwork, so the player bar can show it."""
        if (
                self.current_playlist is not None
                and self.current_playlist["id"] == playlist["id"]
                and self.stack.get_visible_child_name() == "details"
        ):
            return self.track_sort.sort(self.current_tracks)

        by_path = {track.get("path"): track for track in self.tracks}
        tracks = self.database.get_playlist_tracks(playlist["id"])

        for track in tracks:
            cached = by_path.get(track.get("path"), {})
            track["artwork_path"] = cached.get("artwork_path")
            track["album_artwork_path"] = cached.get("album_artwork_path")

        return TrackSort().sort(tracks)

    def _play_playlist(self, playlist):
        if self.playback_manager is None:
            return

        tracks = self._playlist_queue(playlist)
        if tracks:
            self.playback_manager.play_queue(
                tracks,
                0,
                self._playlist_source(playlist),
            )

        self._update_play_buttons()

    def play_page(self):
        """Play the playlist whose page is open, like its Play button does.

        False (and nothing played) when no playlist page is showing or the
        playlist is empty.
        """
        if (
                self.playback_manager is None
                or self.current_playlist is None
                or self.stack.get_visible_child_name() != "details"
                or not self.current_tracks
        ):
            return False

        self._play_playlist(self.current_playlist)
        return True

    def _on_play_clicked(self, button):
        if self.current_playlist is not None:
            self._toggle_playlist(self.current_playlist)

    def _on_track_activated(self, list_box, row):
        if self.playback_manager is None:
            return

        tracks = self.track_sort.sort(self.current_tracks)

        for index, track in enumerate(tracks):
            if track.get("id") == row.track.get("id"):
                self.playback_manager.play_queue(
                    tracks,
                    index,
                    self._playlist_source(self.current_playlist),
                )
                break

    def _on_delete_clicked(self, button):
        if self.current_playlist is not None:
            self._delete_playlist(self.current_playlist)

    def _delete_playlist(self, playlist):
        dialog = Adw.MessageDialog.new(
            self.get_root(),
            "Delete playlist?",
            f'Are you sure you want to delete "{playlist["name"]}"?',
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance(
            "delete",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _dialog, response: self._on_delete_response(
                playlist, response
            ),
        )
        dialog.present()

    def _on_delete_response(self, playlist, response):
        if response != "delete":
            return

        self.database.delete_playlist(playlist["id"])

        if (
                self.current_playlist is not None
                and self.current_playlist["id"] == playlist["id"]
        ):
            self.current_playlist = None
            self.current_tracks = []

        self.refresh()

    # ------------------------------------------------------------------
    # Playlist actions
    # ------------------------------------------------------------------

    def _add_track_to_another_playlist(self, track, playlist_id):
        if self.database is not None:
            self.database.add_track_to_playlist(playlist_id, track["id"])
            self.refresh()

    def _remove_track(self, playlist_id, track):
        self.database.remove_track_from_playlist(playlist_id, track["id"])
        self.refresh()

    def _on_create_clicked(self, button):
        if self.on_create_playlist is not None:
            self.on_create_playlist()

    def _on_rename_clicked(self):
        if (
                self.current_playlist is not None
                and self.on_rename_playlist is not None
        ):
            self.on_rename_playlist(self.current_playlist)

    def _on_import_clicked(self):
        if self.current_playlist is not None and self.on_import is not None:
            self.on_import({"playlist_id": self.current_playlist["id"]})

    def _on_change_cover_clicked(self):
        if (
                self.current_playlist is not None
                and self.on_change_image is not None
        ):
            self.on_change_image(self.current_playlist)

    # ------------------------------------------------------------------
    # Navigation and sorting
    # ------------------------------------------------------------------

    def update_play_count(self, track_id, count):
        """A track was played once more: update its row in the playlist's
        list."""
        update_play_count_in_list(self.track_list, track_id, count)

        for track in self.current_tracks:
            if track.get("id") == track_id:
                track["play_count"] = count

    def _on_back_clicked(self, button):
        # No playlist is open any more: a refresh must not bring it back.
        self.current_playlist = None
        self.current_tracks = []
        self.stack.set_visible_child_name("grid")

    @staticmethod
    def _format_duration(duration):
        duration = max(0, int(duration or 0))
        minutes, seconds = divmod(duration, 60)
        return f"{minutes}:{seconds:02}"