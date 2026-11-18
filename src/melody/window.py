import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib

from melody.services import LibraryManager, LibraryEditor, CollectionMergeRequired
from melody.services.collection_actions import show_text_dialog, show_merge_confirmation, show_error_dialog
from melody.services.artwork import (
    get_cached_artwork,
    get_cached_full_artwork,
)
from melody.services.playback_manager import PlaybackManager
from melody.views import (
    LibraryView,
    AlbumsView,
    ArtistsView,
    PlaylistsView,
    PreferencesView,
)
from melody.widgets.player_bar import PlayerBar


class MelodyWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Melody")

        self.set_default_size(1100, 720)

        self.tracks = []
        self._sync_thread = None
        self.playback_manager = PlaybackManager()
        self.library_manager = LibraryManager()
        self.database = self.library_manager.database
        self.library_editor = LibraryEditor(
            self.library_manager.music_folder,
            self.database,
        )

        self.views = {
            "Library": LibraryView(),
            "Albums": AlbumsView(),
            "Artists": ArtistsView(),
            "Playlists": PlaylistsView(),
            "Preferences": PreferencesView(),
        }

        for name in ("Library", "Albums", "Artists"):
            self.views[name].set_playback_manager(self.playback_manager)

        self.views["Library"].set_context_actions(
            self.database.get_playlists,
            self._add_track_to_playlist,
            self._create_playlist_for_track,
        )
        self.views["Albums"].set_context_actions(
            self.database.get_playlists,
            self._add_track_to_playlist,
            self._create_playlist_for_track,
        )
        self.views["Artists"].set_context_actions(
            self.database.get_playlists,
            self._add_track_to_playlist,
            self._create_playlist_for_track,
        )
        self.views["Albums"].set_rename_callbacks(
            self._rename_album,
            self._rename_album_artist,
        )
        self.views["Artists"].set_rename_callbacks(
            self._rename_artist,
            self._rename_album,
            self._rename_album_artist,
        )
        self.views["Playlists"].set_callbacks(
            self._create_playlist,
            self._rename_playlist,
            self._add_track_to_playlist,
        )

        self.navigation_rows = {}

        self.navigation = Adw.NavigationSplitView()
        self.navigation.set_sidebar_width_fraction(0.22)
        self.navigation.set_min_sidebar_width(180)
        self.navigation.set_max_sidebar_width(280)

        self.navigation.set_sidebar(self._create_sidebar())
        self.navigation.set_content(self._create_content())

        self.set_content(self.navigation)
        self.connect("close-request", self._on_close_request)

        self._select_page("Library")
        self._start_library_sync()

    def _create_sidebar(self):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        sidebar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )

        title = Gtk.Label(label="Melody", xalign=0)
        title.add_css_class("title-2")
        title.set_margin_start(18)
        title.set_margin_top(18)
        title.set_margin_bottom(18)
        sidebar.append(title)

        self.navigation_list = Gtk.ListBox()
        self.navigation_list.set_selection_mode(
            Gtk.SelectionMode.SINGLE
        )
        self.navigation_list.add_css_class("navigation-sidebar")
        self.navigation_list.connect(
            "row-selected",
            self._on_navigation_selected,
        )

        for name, icon in [
            ("Library", "audio-x-generic-symbolic"),
            ("Albums", "media-optical-symbolic"),
            ("Artists", "system-users-symbolic"),
            ("Playlists", "view-list-symbolic"),
        ]:
            row = self._create_navigation_row(name, icon)
            self.navigation_rows[name] = row
            self.navigation_list.append(row)

        sidebar.append(self.navigation_list)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        preferences_row = self._create_navigation_row(
            "Preferences",
            "emblem-system-symbolic",
        )
        self.navigation_rows["Preferences"] = preferences_row
        sidebar.append(preferences_row)

        toolbar.set_content(sidebar)
        return Adw.NavigationPage.new(toolbar, "Navigation")

    @staticmethod
    def _create_navigation_row(title, icon):
        row = Gtk.ListBoxRow()

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)

        image = Gtk.Image.new_from_icon_name(icon)
        label = Gtk.Label(label=title, xalign=0)
        label.set_hexpand(True)

        content.append(image)
        content.append(label)
        row.set_child(content)
        return row

    def _create_content(self):
        self.content_toolbar = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        self.content_toolbar.add_top_bar(self.content_header)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )
        self.content_stack.set_vexpand(True)

        for name, view in self.views.items():
            self.content_stack.add_named(view, name.lower())

        self.sync_status = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.sync_status.set_margin_start(12)
        self.sync_status.set_margin_end(12)
        self.sync_status.set_margin_top(6)
        self.sync_status.set_margin_bottom(6)

        self.sync_spinner = Gtk.Spinner()
        self.sync_spinner.start()

        self.sync_status_label = Gtk.Label(
            label="Scanning music library…",
            xalign=0,
        )
        self.sync_status_label.set_hexpand(True)

        self.sync_status.append(self.sync_spinner)
        self.sync_status.append(self.sync_status_label)

        self.player_bar = PlayerBar(self.playback_manager)
        self.playback_manager.on_track_changed = self.player_bar.set_track
        self.playback_manager.on_state_changed = (
            self.player_bar.update_playback_state
        )
        self.playback_manager.on_error = self._on_playback_error

        layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        layout.append(self.content_stack)
        layout.append(self.sync_status)
        layout.append(self.player_bar)

        self.content_toolbar.set_content(layout)
        return Adw.NavigationPage.new(self.content_toolbar, "Content")

    def _on_navigation_selected(self, list_box, row):
        if row is None:
            return

        for name, navigation_row in self.navigation_rows.items():
            if navigation_row is row:
                self._select_page(name)
                break

    def _select_page(self, name):
        self.content_stack.set_visible_child_name(name.lower())
        self.content_header.set_title_widget(
            Gtk.Label(label=name)
        )

        if name in self.navigation_rows:
            self.navigation_list.select_row(
                self.navigation_rows[name]
            )

    def _start_library_sync(self):
        self._sync_thread = threading.Thread(
            target=self._sync_library,
            daemon=True,
        )
        self._sync_thread.start()

    @staticmethod
    def _album_key(track):
        return (
            str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            ).casefold(),
            str(track.get("album") or "Unknown Album").casefold(),
        )

    def _sync_library(self):
        try:
            report = self.library_manager.sync()
            tracks = self.database.get_tracks()

            album_artwork = {}

            for track in tracks:
                track_path = Path(track["path"])
                artist = (
                        track.get("album_artist")
                        or track.get("artist")
                        or "Unknown Artist"
                )
                album = track.get("album") or "Unknown Album"

                track["artwork_path"] = get_cached_artwork(
                    track_path,
                    artist,
                    album,
                )

                album_key = self._album_key(track)

                if album_key not in album_artwork:
                    album_artwork[album_key] = get_cached_full_artwork(
                        track_path,
                        artist,
                        album,
                    )

                track["album_artwork_path"] = album_artwork[album_key]

        except Exception as error:
            GLib.idle_add(
                self._on_sync_failed,
                str(error),
            )
            return

        GLib.idle_add(
            self._on_sync_finished,
            report,
            tracks,
        )

    def _on_sync_finished(self, report, tracks):
        self.tracks = tracks
        self.playback_manager.set_queue(tracks)

        self.views["Library"].set_tracks(tracks)
        self.views["Albums"].set_tracks(tracks)
        self.views["Artists"].set_tracks(tracks)
        self.views["Playlists"].set_data(
            self.database,
            tracks,
            self.playback_manager,
        )

        self.sync_spinner.stop()
        self.sync_spinner.set_visible(False)

        track_count = len(tracks)
        error_count = len(report.errors)

        if error_count:
            self.sync_status_label.set_text(
                f"{track_count} tracks loaded • {error_count} issue(s)"
            )
            self.sync_status.set_tooltip_text(
                "\n".join(report.errors)
            )
        else:
            self.sync_status_label.set_text(
                f"{track_count} tracks loaded"
            )
            GLib.timeout_add_seconds(
                4,
                self._hide_sync_status,
            )

        return GLib.SOURCE_REMOVE

    def _add_track_to_playlist(self, track, playlist_id):
        try:
            self.database.add_track_to_playlist(playlist_id, track["id"])
            self.views["Playlists"].refresh()
        except Exception as error:
            show_error_dialog(self, "Could not add track", error)

    def _create_playlist_for_track(self, track):
        def create(name):
            try:
                playlist_id = self.database.create_playlist(name)
                self.database.add_track_to_playlist(playlist_id, track["id"])
                self.views["Playlists"].refresh()
            except Exception as error:
                show_error_dialog(self, "Could not create playlist", error)

        show_text_dialog(self, "Create playlist", "Choose a name for the new playlist.", "", create)

    def _create_playlist(self):
        def create(name):
            try:
                playlist_id = self.database.create_playlist(name)
                self.views["Playlists"].refresh()
                playlist = self.database.get_playlist(playlist_id)
                if playlist:
                    self.views["Playlists"]._show_playlist(playlist)
            except Exception as error:
                show_error_dialog(self, "Could not create playlist", error)

        show_text_dialog(self, "Create playlist", "Choose a name for the new playlist.", "", create)

    def _rename_playlist(self, playlist):
        def rename(name):
            try:
                self.database.rename_playlist(playlist["id"], name)
                self.views["Playlists"].refresh()
            except Exception as error:
                show_error_dialog(self, "Could not rename playlist", error)

        show_text_dialog(self, "Rename playlist", "Enter the new playlist name.", playlist["name"], rename)

    def _rename_album(self, album):
        def prompt():
            def rename(new_name):
                self._perform_collection_rename(
                    lambda allow: self.library_editor.rename_album(
                        album["artist"], album["title"], new_name,
                        allow_merge=allow,
                    ),
                    "album",
                )
            show_text_dialog(
                self, "Rename album", "Enter the new album title.",
                album["title"], rename,
            )
        prompt()

    def _rename_album_artist(self, album):
        def rename(new_artist):
            # Same title, new artist: the editor rewrites the tags and moves
            # the files into the new artist's folder (asking before merging
            # into an existing album).
            self._perform_collection_rename(
                lambda allow: self.library_editor.rename_album(
                    album["artist"], album["title"], album["title"],
                    new_artist=new_artist,
                    allow_merge=allow,
                ),
                "album",
            )

        show_text_dialog(
            self, "Change album artist",
            "Enter the new artist for this album. Its files will be moved "
            "to that artist's folder.",
            album["artist"], rename,
        )

    def _rename_artist(self, artist):
        def rename(new_name):
            self._perform_collection_rename(
                lambda allow: self.library_editor.rename_artist(
                    artist["name"], new_name, allow_merge=allow,
                ),
                "artist",
            )
        show_text_dialog(
            self, "Rename artist", "Enter the new artist name.",
            artist["name"], rename,
        )

    def _perform_collection_rename(self, operation, collection_type):
        try:
            result = operation(False)
        except CollectionMergeRequired as merge:
            show_merge_confirmation(
                self,
                collection_type,
                merge.name,
                lambda: self._confirm_collection_rename(operation, collection_type),
            )
            return
        except Exception as error:
            show_error_dialog(self, f"Could not rename {collection_type}", error)
            return
        self._finish_collection_rename(result, collection_type)

    def _confirm_collection_rename(self, operation, collection_type):
        try:
            result = operation(True)
        except Exception as error:
            show_error_dialog(self, f"Could not rename {collection_type}", error)
            return
        self._finish_collection_rename(result, collection_type)

    def _finish_collection_rename(self, result, collection_type):
        if result.get("errors"):
            show_error_dialog(
                self,
                f"Some {collection_type} files could not be updated",
                "\n".join(result["errors"]),
            )
        self._reload_library_from_database()

    def _reload_library_from_database(self):
        previous = {track["id"]: track for track in self.tracks}
        tracks = self.database.get_tracks()
        for track in tracks:
            old = previous.get(track["id"], {})
            track["artwork_path"] = old.get("artwork_path")
            track["album_artwork_path"] = old.get("album_artwork_path")
        self.tracks = tracks
        self.playback_manager.set_queue(tracks)
        self.views["Library"].set_tracks(tracks)
        self.views["Albums"].set_tracks(tracks)
        self.views["Artists"].set_tracks(tracks)
        self.views["Playlists"].set_data(self.database, tracks, self.playback_manager)

    def _on_sync_failed(self, message):
        self.sync_spinner.stop()
        self.sync_spinner.set_visible(False)

        self.sync_status_label.set_text(
            "Could not synchronize music library"
        )
        self.sync_status.set_tooltip_text(message)
        return GLib.SOURCE_REMOVE

    def _on_playback_error(self, message):
        self.sync_status.set_visible(True)
        self.sync_status_label.set_text(
            f"Playback error: {message}"
        )

    def _hide_sync_status(self):
        self.sync_status.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, window):
        self.playback_manager.close()
        return False