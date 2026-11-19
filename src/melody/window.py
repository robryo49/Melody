import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib

from melody.services import LibraryManager, LibraryEditor, CollectionMergeRequired
from melody.services.collection_actions import (
    show_edit_album_dialog,
    show_edit_artist_dialog,
    show_edit_playlist_dialog,
    show_edit_track_dialog,
    show_error_dialog,
    show_merge_confirmation,
    show_playlist_picker_dialog,
    show_text_dialog,
    set_track_navigation,
    set_track_deletion,
)
from melody.services.artwork import (
    get_album_artwork_bytes,
    get_cached_album_artwork,
    get_cached_artwork,
    get_local_artist_artwork,
)
from melody.services.library_scanner import _sanitize_name
from melody.services.image_sources import album_sources, artist_sources
from melody.services.playlist_artwork import set_playlist_cover
from melody.services.playback_manager import PlaybackManager
from melody.views import (
    LibraryView,
    AlbumsView,
    ArtistsView,
    PlaylistsView,
    show_preferences_dialog,
)
from melody.services.compilations import is_compilation
from melody.services.settings import get_settings
from melody.import_sources import get_import_sources
from melody.services.metadata import MetadataError
from melody.widgets.image_picker import show_image_picker
from melody.widgets.import_dialogs import (
    pick_audio_files,
    show_import_dialog,
    show_import_preview,
    show_link_dialog,
    show_search_dialog,
)
from melody.widgets.player_bar import PlayerBar


class MelodyWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Melody")

        self.set_default_size(1100, 720)

        self.tracks = []
        self._sync_thread = None
        # True while a library scan is running; ``_rescan_done`` is told when
        # the scan a Preferences "Rescan" button started is over.
        self._syncing = False
        self._rescan_done = None
        # Downloads waiting in temporary folders until they have been
        # imported (see ``_discard_downloads``).
        self._downloads = []
        self.playback_manager = PlaybackManager()
        self.library_manager = LibraryManager()
        self.database = self.library_manager.database
        self.library_editor = LibraryEditor(
            self.library_manager.music_folder,
            self.database,
        )

        self.settings = get_settings()

        self.views = {
            # With compilations enabled the Library only has single tracks,
            # and the compilations have a page of their own.
            "Library": LibraryView(
                track_filter=lambda track: not is_compilation(track),
            ),
            "Compilations": LibraryView(
                title="Compilations",
                noun="compilation",
                search_placeholder="Search compilations…",
                empty_title="No compilations",
                empty_description=(
                    "Long audio files such as mixes and live sets "
                    "appear here."
                ),
                empty_icon="media-playlist-consecutive-symbolic",
                track_filter=is_compilation,
            ),
            "Albums": AlbumsView(),
            "Artists": ArtistsView(),
            "Playlists": PlaylistsView(),
        }

        for name in ("Library", "Compilations", "Albums", "Artists"):
            self.views[name].set_playback_manager(self.playback_manager)

        for name in ("Library", "Compilations", "Albums", "Artists"):
            self.views[name].set_context_actions(
                self._edit_track,
                self._choose_playlist_for_track,
            )
        # The Library can import music.
        self.views["Library"].set_import_callback(self._import_music)
        # The pages of an album, an artist and a playlist can too, and what
        # they import starts out belonging to them.
        for name in ("Albums", "Artists", "Playlists"):
            self.views[name].set_import_callback(self._import_music)
        set_track_deletion(self._delete_track)
        self.views["Albums"].set_rename_callbacks(
            self._edit_album,
            self._edit_album,
        )
        self.views["Artists"].set_rename_callbacks(
            self._edit_artist,
            self._edit_album,
            self._edit_album,
        )
        self.views["Albums"].set_image_callbacks(self._change_album_image)
        self.views["Artists"].set_image_callbacks(
            self._change_artist_image,
            self._change_album_image,
        )
        set_track_navigation(
            self._open_album_of_track,
            self._open_artist_of_track,
            self._is_album_of_track_open,
            self._is_artist_of_track_open,
        )
        self.views["Playlists"].set_callbacks(
            self._create_playlist,
            self._edit_playlist,
            self._choose_playlist_for_track,
            self._edit_track,
        )
        self.views["Playlists"].set_image_callback(
            self._change_playlist_image,
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

        self._update_compilations_ui()
        self.settings.connect_changed(self._on_settings_changed)

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
            # Only shown while compilations are enabled.
            ("Compilations", "media-playlist-consecutive-symbolic"),
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

        # Not a page: it opens the preferences in a window of their own.
        preferences_list = Gtk.ListBox()
        preferences_list.set_selection_mode(Gtk.SelectionMode.NONE)
        preferences_list.set_activate_on_single_click(True)
        preferences_list.add_css_class("navigation-sidebar")
        preferences_list.connect(
            "row-activated",
            lambda _list_box, _row: self._show_preferences(),
        )
        preferences_list.append(preferences_row)
        sidebar.append(preferences_list)

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
        self.player_bar.on_play_idle = self._play_selected_track

        layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        layout.append(self.content_stack)
        layout.append(self.sync_status)
        layout.append(self.player_bar)

        self.content_toolbar.set_content(layout)
        return Adw.NavigationPage.new(self.content_toolbar, "Content")

    def _show_preferences(self):
        show_preferences_dialog(self, self.settings, self._rescan_library)

    def _update_compilations_ui(self):
        """The Compilations page only exists while compilations are enabled."""
        enabled = self.settings.compilations_enabled
        self.navigation_rows["Compilations"].set_visible(enabled)

        if (
                not enabled
                and self.content_stack.get_visible_child_name() == "compilations"
        ):
            self._select_page("Library")

    def _on_settings_changed(self):
        # Which tracks go where depends on the settings: sort them out again.
        self._update_compilations_ui()

        for name in ("Library", "Compilations", "Albums", "Artists"):
            self.views[name].set_tracks(self.tracks)

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
        self._syncing = True
        self._sync_thread = threading.Thread(
            target=self._sync_library,
            daemon=True,
        )
        self._sync_thread.start()

    def _rescan_library(self, done=None):
        """Scan the library again: every file whose path does not match its
        tags is moved where it belongs, and the pages are refreshed.

        ``done()`` is called when the scan is over. If one is already
        running, no second one is started and ``done`` waits for that one.
        Always returns True.
        """
        self._rescan_done = done

        if self._syncing:
            return True

        self.sync_status.set_visible(True)
        self.sync_status.set_tooltip_text(None)
        self.sync_spinner.set_visible(True)
        self.sync_spinner.start()
        self.sync_status_label.set_text("Scanning music library…")

        self._start_library_sync()
        return True

    def _finish_sync(self):
        self._syncing = False

        done, self._rescan_done = self._rescan_done, None

        if done is not None:
            done()

    @staticmethod
    def _album_key(track):
        artist = str(
            track.get("album_artist")
            or track.get("artist")
            or "Unknown Artist"
        ).casefold()
        album = str(track.get("album") or "")

        # A single has no album, so no cover to share with other tracks: it
        # is a group of its own.
        if not album.strip():
            return (artist, "", track.get("id"))

        return (artist, album.casefold())

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
                    album_artwork[album_key] = get_cached_album_artwork(
                        track_path,
                        artist,
                        album,
                    )

                track["album_artwork_path"] = album_artwork[album_key]

            self._attach_artist_artwork(tracks)

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
        self.views["Compilations"].set_tracks(tracks)
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

        self._finish_sync()
        return GLib.SOURCE_REMOVE

    def _add_track_to_playlist(self, track, playlist_id):
        try:
            self.database.add_track_to_playlist(playlist_id, track["id"])
            self.views["Playlists"].refresh()
        except Exception as error:
            show_error_dialog(self, "Could not add track", error)

    def _choose_playlist_for_track(self, track, exclude_playlist_id=None):
        playlists = [
            playlist for playlist in self.database.get_playlists()
            if playlist["id"] != exclude_playlist_id
        ]
        show_playlist_picker_dialog(
            self,
            track,
            playlists,
            lambda playlist_id: self._add_track_to_playlist(track, playlist_id),
            lambda: self._create_playlist_for_track(track),
        )

    # ------------------------------------------------------------------
    # "Open album" / "Open artist" in a track's right-click menu
    # ------------------------------------------------------------------

    def _open_album_of_track(self, track):
        if self.views["Albums"].open_album_of_track(track):
            self._select_page("Albums")

    def _open_artist_of_track(self, track):
        if self.views["Artists"].open_artist_of_track(track):
            self._select_page("Artists")

    def _is_album_of_track_open(self, track):
        page = self.content_stack.get_visible_child_name()

        if page == "albums":
            return self.views["Albums"].is_album_open(track)

        if page == "artists":
            return self.views["Artists"].is_album_open(track)

        return False

    def _is_artist_of_track_open(self, track):
        return (
            self.content_stack.get_visible_child_name() == "artists"
            and self.views["Artists"].is_artist_open(track)
        )

    def _edit_track(self, track):
        def save(values):
            image = values.get("image")
            changed = any(
                values[key].strip() != str(track.get(key) or "").strip()
                for key in ("title", "artist", "album")
            )
            result = {"moved": 0, "errors": [], "merged": False}

            # Where the track starts / stops playing. Stored by track id, so
            # it follows the track when the edit below renames or moves it.
            if "start_time" in values and (
                    values["start_time"] != track.get("start_time")
                    or values["end_time"] != track.get("end_time")
            ):
                try:
                    self.database.set_track_range(
                        track["id"],
                        values["start_time"],
                        values["end_time"],
                    )
                except Exception as error:
                    show_error_dialog(
                        self, "Could not save the start and end times", error,
                    )
                    return

                self.playback_manager.update_track_range(
                    track["id"],
                    values["start_time"],
                    values["end_time"],
                )

            if "kind" in values and values["kind"] != track.get("kind"):
                try:
                    self.database.set_track_kind(track["id"], values["kind"])
                except Exception as error:
                    show_error_dialog(
                        self, "Could not save the track's kind", error,
                    )
                    return

            if changed:
                try:
                    result = self.library_editor.edit_track(
                        track["id"],
                        values["title"],
                        values["artist"],
                        values["album"],
                    )
                except Exception as error:
                    show_error_dialog(self, "Could not edit track", error)
                    return

            # The image is written after the rest, so it lands in the file
            # as it is once renamed and moved.
            if image:
                self._apply_image(
                    result,
                    lambda: self.library_editor.set_track_image(
                        track["id"], image,
                    ),
                )

            self._finish_collection_rename(
                result,
                "track",
                refresh_artwork_ids=[track["id"]] if image else (),
            )

        show_edit_track_dialog(
            self, track, save, library_tracks=self.tracks,
        )

    def _delete_track(self, track):
        """The "Delete" entry of a track's menu: ask, then move the file to the
        trash and take the track out of the library and its playlists."""
        # A scan running at the same time could bring the track back, or
        # replace the pages with a list that still has it.
        if self._syncing:
            self._show_library_busy()
            return

        title = str(track.get("title") or "this track")

        dialog = Adw.MessageDialog.new(
            self,
            "Delete track?",
            f"“{title}” will be moved to the trash and removed from your "
            "library and from its playlists.",
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
            lambda _dialog, response: self._on_delete_track_response(
                track, response,
            ),
        )
        dialog.present()

    def _on_delete_track_response(self, track, response):
        if response != "delete":
            return

        try:
            result = self.library_editor.delete_track(track["id"])
        except Exception as error:
            show_error_dialog(self, "Could not delete track", error)
            return

        # A deleted track must not keep playing.
        current = self.playback_manager.current_track

        if current is not None and current.get("id") == track.get("id"):
            self.playback_manager.stop()
            self.player_bar.set_track(None)

        if result.get("errors"):
            show_error_dialog(
                self,
                "The track was deleted, but not everything was cleaned up",
                "\n".join(result["errors"]),
            )

        self._reload_library_from_database()

    # ------------------------------------------------------------------
    # Importing music
    # ------------------------------------------------------------------

    def _import_music(self, context=None):
        """An Import button: ask how, then import.

        ``context`` says where the button was: ``artist`` and ``album`` are
        the starting values of what is imported, and ``playlist_id`` is a
        playlist to add it to. The Library has no context.
        """
        # Importing writes to the library and the database, which a scan
        # running at the same time would undo.
        if self._syncing:
            self._show_library_busy()
            return

        context = dict(context or {})
        show_import_dialog(
            self,
            get_import_sources(),
            lambda source: self._import_from(source, context),
        )

    def _show_library_busy(self):
        show_error_dialog(
            self,
            "The library is busy",
            "Wait for the library scan to finish, then try again.",
        )

    def _import_from(self, source, context):
        """Go on with the import source the person picked: each ``kind`` of
        source has its own windows (see ``melody.import_sources``)."""
        if source.kind == "files":
            self._import_from_files(source, context)
        elif source.kind == "link":
            self._import_from_link(source, context)
        elif source.kind == "search":
            self._import_from_search(source, context)

    def _import_from_files(self, source, context):
        pick_audio_files(
            self,
            lambda paths: self._on_import_files_picked(source, paths, context),
        )

    def _on_import_files_picked(self, source, paths, context):
        pending, errors = source.load(paths)
        self._review_pending_tracks(pending, errors, context)

    def _import_from_search(self, source, context):
        # Picking a result goes on like a pasted link, with its address
        # already filled in.
        show_search_dialog(
            self,
            source,
            lambda result: self._import_from_link(
                source,
                context,
                url=result.url,
            ),
        )

    def _import_from_link(self, source, context, url=None):
        show_link_dialog(
            self,
            source,
            lambda download: self._on_downloaded(source, download, context),
            discard=lambda download: download.cleanup(),
            initial_url=url,
        )

    def _on_downloaded(self, source, download, context):
        try:
            pending = [source.to_pending(download)]
            errors = []
        except (MetadataError, OSError) as error:
            download.cleanup()
            pending = []
            errors = [f"{download.path.name}: {error}"]
        else:
            # The audio stays in its temporary folder until it has been
            # copied into the library (or the import is given up).
            self._downloads.append(download)

        self._review_pending_tracks(pending, errors, context)

    def _review_pending_tracks(self, pending, errors, context):
        """Show what is about to be imported, whether it came from files or
        from a link: one track goes straight to its edit window, several to
        a preview of them all."""
        if not pending:
            show_error_dialog(
                self,
                "Nothing to import",
                "\n".join(errors) or "No file was selected.",
            )
            return

        # Start from the page the import was made from. The fields stay
        # editable in the windows below.
        for item in pending:
            item.set_defaults(
                artist=context.get("artist"),
                album=context.get("album"),
            )

        playlist_id = context.get("playlist_id")

        if len(pending) == 1:
            # One file: straight to its edit window. Save adds it to the
            # library, Cancel adds nothing.
            item = pending[0]

            def save(values):
                item.apply(values)
                self._import_tracks(pending, errors, playlist_id)

            show_edit_track_dialog(
                self,
                item.as_track(),
                save,
                library_tracks=self.tracks,
                window_title="Import track",
                hint=(
                    "The file is copied into your library, named after "
                    "its title, artist and album. Leave the album empty "
                    "for a single: it is then kept in the artist's folder."
                ),
                allow_unchanged=True,
            )
            return

        # Several files: a preview of them all, each editable, then Save adds
        # every one of them.
        show_import_preview(
            self,
            pending,
            self.tracks,
            lambda items: self._import_tracks(items, errors, playlist_id),
        )

    def _import_tracks(self, pending, earlier_errors=(), playlist_id=None):
        """Copy the pending files into the library and refresh the pages.

        ``earlier_errors`` are files that could not even be read: they are
        reported together with the ones that fail here. With ``playlist_id``,
        every track that makes it into the library is added to that playlist.
        """
        if self._syncing:
            self._show_library_busy()
            return

        errors = list(earlier_errors)
        imported = 0

        for item in pending:
            try:
                result = self.library_editor.import_track(
                    item.path,
                    item.final_metadata(),
                    image=item.image,
                    start_time=item.start_time,
                    end_time=item.end_time,
                    kind=item.kind,
                )
            except Exception as error:
                errors.append(str(error))
                continue

            imported += 1
            errors.extend(result["errors"])

            if playlist_id is not None:
                try:
                    self.database.add_track_to_playlist(
                        playlist_id, result["id"],
                    )
                except Exception as error:
                    errors.append(
                        f"{item.path.name}: could not add it to the "
                        f"playlist: {error}"
                    )

        self._discard_downloads()

        if imported:
            # The new tracks show up at once. Their artwork is looked up by
            # a scan in the background (it may need the network, which must
            # not freeze the window).
            self._reload_library_from_database()
            self._rescan_library()

        if errors:
            show_error_dialog(
                self,
                (
                    "Some files could not be imported"
                    if imported else "Could not import"
                ),
                "\n".join(errors),
            )

    def _discard_downloads(self):
        """Delete the temporary folders of the downloads: what they hold has
        been copied into the library, or the import was cancelled."""
        downloads, self._downloads = self._downloads, []

        for download in downloads:
            download.cleanup()

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

    def _edit_playlist(self, playlist):
        def save(values):
            image = values.get("image")

            try:
                if values["name"] != playlist["name"]:
                    self.database.rename_playlist(
                        playlist["id"], values["name"],
                    )
            except Exception as error:
                show_error_dialog(self, "Could not edit playlist", error)
                return

            if image:
                try:
                    set_playlist_cover(self.database, playlist["id"], image)
                except Exception as error:
                    show_error_dialog(
                        self, "Could not change the playlist's image", error,
                    )

            self.views["Playlists"].refresh()

        show_edit_playlist_dialog(
            self,
            playlist,
            self.views["Playlists"].cover_for(playlist),
            save,
        )

    def _change_playlist_image(self, playlist):
        """The edit button over a playlist's picture: pick, then apply right
        away (there is no Save step outside the edit window). A playlist has
        no suggestions: the person picks an image file."""
        def apply(data):
            try:
                set_playlist_cover(self.database, playlist["id"], data)
            except Exception as error:
                show_error_dialog(
                    self, "Could not change the playlist's image", error,
                )
                return

            self.views["Playlists"].refresh()

        show_image_picker(self, [], apply)

    def _edit_album(self, album):
        def save(values):
            image = values.get("image")
            apply_to_tracks = bool(values.get("apply_to_tracks"))
            renamed = (
                values["title"] != album["title"]
                or values["artist"] != album["artist"]
            )
            track_ids = [track["id"] for track in album["tracks"]]

            # What "Apply artwork" writes into the tracks: the newly picked
            # image, or else the album's current artwork. Read now, before a
            # rename moves the files.
            artwork_data = image
            if apply_to_tracks and artwork_data is None:
                artwork_data = self._album_artwork_bytes(album)

                if artwork_data is None:
                    show_error_dialog(
                        self,
                        "Could not apply artwork",
                        "This album has no artwork to apply to its tracks.",
                    )
                    return

            def operation(allow):
                result = {"moved": 0, "errors": [], "merged": False}

                # One call handles a new title, a new album artist or both:
                # the editor rewrites the tags, moves the files, and asks
                # before merging into an existing album.
                if renamed:
                    result = self.library_editor.rename_album(
                        album["artist"], album["title"], values["title"],
                        new_artist=values["artist"],
                        allow_merge=allow,
                    )

                # Then the artwork goes to the album under its new name: a
                # newly picked image becomes its cover.jpg, and "Apply
                # artwork" also writes the artwork into every track.
                if artwork_data is not None:
                    self._apply_image(
                        result,
                        lambda: self.library_editor.set_album_image(
                            values["artist"],
                            values["title"],
                            artwork_data,
                            apply_to_tracks=apply_to_tracks,
                            write_folder=image is not None,
                        ),
                    )

                return result

            self._perform_collection_rename(
                operation,
                "album",
                refresh_artwork_ids=(
                    track_ids if artwork_data is not None else ()
                ),
            )

        show_edit_album_dialog(self, album, self.tracks, save)

    def _edit_artist(self, artist):
        def save(values):
            image = values.get("image")
            renamed = values["name"] != artist["name"]

            def operation(allow):
                result = {"moved": 0, "errors": [], "merged": False}

                if renamed:
                    result = self.library_editor.rename_artist(
                        artist["name"], values["name"], allow_merge=allow,
                    )

                if image:
                    self._apply_image(
                        result,
                        lambda: self.library_editor.set_artist_image(
                            values["name"], image,
                        ),
                    )

                return result

            self._perform_collection_rename(operation, "artist")

        show_edit_artist_dialog(self, artist, self.tracks, save)

    @staticmethod
    def _album_artwork_bytes(album):
        """The album's current artwork at full size (None if it has none)."""
        for track in album["tracks"]:
            try:
                data = get_album_artwork_bytes(Path(track["path"]))
            except Exception:
                data = None

            if data:
                return data

        # Fall back on the 256px copy generated for the grid.
        for track in album["tracks"]:
            cached = track.get("album_artwork_path")

            if cached:
                try:
                    return Path(cached).read_bytes()
                except OSError:
                    continue

        return None

    def _change_album_image(self, album):
        """The edit button over an album's cover: pick, then apply right away
        (there is no Save step outside the edit window). The image becomes the
        album's cover; the tracks keep their own images (the edit window's
        "Apply artwork" button writes it into them)."""
        track_ids = [track["id"] for track in album["tracks"]]

        def apply(data):
            result = {"errors": []}
            self._apply_image(
                result,
                lambda: self.library_editor.set_album_image(
                    album["artist"],
                    album["title"],
                    data,
                    apply_to_tracks=False,
                ),
            )
            self._finish_collection_rename(
                result, "album", refresh_artwork_ids=track_ids,
            )

        show_image_picker(
            self,
            album_sources(album["artist"], album["title"]),
            apply,
        )

    def _change_artist_image(self, artist):
        """The edit button over an artist's picture (see above)."""
        def apply(data):
            result = {"errors": []}
            self._apply_image(
                result,
                lambda: self.library_editor.set_artist_image(
                    artist["name"], data,
                ),
            )
            self._finish_collection_rename(result, "artist")

        show_image_picker(self, artist_sources(artist["name"]), apply)

    @staticmethod
    def _apply_image(result, apply):
        """Run ``apply()`` (which writes the chosen image) and fold what went
        wrong into ``result``.

        By then the names have already been changed, so a failure here is
        reported along with the other errors rather than raised, which would
        hide the part that worked.
        """
        try:
            outcome = apply()
        except Exception as error:
            result["errors"].append(f"Image: {error}")
        else:
            result["errors"].extend(outcome.get("errors", []))

    def _perform_collection_rename(
            self,
            operation,
            collection_type,
            refresh_artwork_ids=(),
    ):
        try:
            result = operation(False)
        except CollectionMergeRequired as merge:
            show_merge_confirmation(
                self,
                collection_type,
                merge.name,
                lambda: self._confirm_collection_rename(
                    operation, collection_type, refresh_artwork_ids,
                ),
            )
            return
        except Exception as error:
            show_error_dialog(self, f"Could not rename {collection_type}", error)
            return
        self._finish_collection_rename(
            result, collection_type, refresh_artwork_ids,
        )

    def _confirm_collection_rename(
            self,
            operation,
            collection_type,
            refresh_artwork_ids=(),
    ):
        try:
            result = operation(True)
        except Exception as error:
            show_error_dialog(self, f"Could not rename {collection_type}", error)
            return
        self._finish_collection_rename(
            result, collection_type, refresh_artwork_ids,
        )

    def _finish_collection_rename(
            self,
            result,
            collection_type,
            refresh_artwork_ids=(),
    ):
        if result.get("errors"):
            show_error_dialog(
                self,
                f"Some {collection_type} files could not be updated",
                "\n".join(result["errors"]),
            )
        self._reload_library_from_database(refresh_artwork_ids)

    def _attach_artist_artwork(self, tracks):
        """Give every track the path of its artist's own picture, if any.

        That is an image in the artist's folder (``artist.jpg``...), found
        without touching the network.
        """
        pictures = {}

        for track in tracks:
            name = str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            )
            key = name.casefold()

            if key not in pictures:
                pictures[key] = get_local_artist_artwork(
                    self.library_manager.music_folder / _sanitize_name(name)
                )

            track["artist_artwork_path"] = pictures[key]

    def _refresh_artwork(self, tracks, track_ids):
        """Regenerate the artwork of ``track_ids`` after their image changed.

        Only those tracks' thumbnails are redone. The cover of each album they
        belong to is redone the way the library scan does it (from the album's
        first track), so editing one track never makes the album show a
        different cover unless that track is the one the album takes it from.
        """
        first_track = {}
        for track in tracks:
            first_track.setdefault(self._album_key(track), track)

        album_covers = {}

        for track in tracks:
            if track["id"] not in track_ids:
                continue

            artist = (
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            )
            album = track.get("album") or "Unknown Album"

            track["artwork_path"] = get_cached_artwork(
                Path(track["path"]), artist, album,
            )

            key = self._album_key(track)
            if key not in album_covers:
                album_covers[key] = get_cached_album_artwork(
                    Path(first_track[key]["path"]), artist, album,
                )

        for track in tracks:
            key = self._album_key(track)
            if key in album_covers:
                track["album_artwork_path"] = album_covers[key]

    def _reload_library_from_database(self, refresh_artwork_ids=()):
        previous = {track["id"]: track for track in self.tracks}
        tracks = self.database.get_tracks()
        for track in tracks:
            old = previous.get(track["id"], {})
            track["artwork_path"] = old.get("artwork_path")
            track["album_artwork_path"] = old.get("album_artwork_path")

        if refresh_artwork_ids:
            self._refresh_artwork(tracks, set(refresh_artwork_ids))

        self._attach_artist_artwork(tracks)
        self.tracks = tracks
        self.playback_manager.set_queue(tracks)
        self.views["Library"].set_tracks(tracks)
        self.views["Compilations"].set_tracks(tracks)
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
        self._finish_sync()
        return GLib.SOURCE_REMOVE

    def _play_selected_track(self):
        """Play button with nothing loaded: play the track selected on the
        page showing (Library or Compilations), like a double click on it.

        False when there is no such track, so the button does what it
        normally does.
        """
        page = self.content_stack.get_visible_child_name()

        if page in ("library", "compilations"):
            return self.views[page.capitalize()].play_selected()

        return False

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
        self._discard_downloads()
        return False