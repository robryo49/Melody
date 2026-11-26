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
    show_new_playlist_dialog,
    show_text_dialog,
    show_set_album_dialog,
    show_set_artist_dialog,
    set_track_navigation,
    set_track_deletion,
    set_track_queue_actions,
    set_tracks_actions,
    set_artwork_actions,
    attach_track_context_menu,
    track_is_playing,
    copy_image_to_clipboard,
    read_clipboard_image,
)
from melody.services.artwork import (
    get_album_artwork_bytes,
    get_cached_album_artwork,
    get_cached_artwork,
    get_local_artist_artwork,
    get_local_artist_artwork_bytes,
    get_track_artwork_bytes,
)
from melody.services.library_scanner import _sanitize_name
from melody.services.image_sources import (
    album_search,
    artist_search,
    playlist_search,
)
from melody.services.playlist_artwork import (
    get_playlist_cover,
    set_playlist_cover,
)
from melody.services.playlist_categories import (
    clean_category,
    same_category,
)
from melody.services.playback_manager import PlaybackManager
from melody.services.recent_tracks import select_recent_tracks
from melody.services.most_played_tracks import select_most_played_tracks
from melody.services.mpris import MprisService
from melody.services.keyboard_shortcuts import KeyboardShortcuts
from melody.services.duplicate_check import find_duplicates
from melody.services.library_watcher import LibraryWatcher
from melody.views import (
    LibraryView,
    AlbumsView,
    ArtistsView,
    PlaylistsView,
    SearchView,
    QueueView,
    NowPlayingView,
    show_preferences_dialog,
)
from melody.services.compilations import is_compilation
from melody.services.genres import same_genres
from melody.services.settings import get_settings
from melody.services.session import load_session, save_session
from melody.services.equalizer import get_equalizer
from melody.services.library_search import artist_name
from melody.services.replaygain_analysis import (
    analyse_tracks,
    analysis_available,
    measure_files,
)
from melody.services.replaygain import (
    ReplayGainError,
    read_track_gain,
    supports_replaygain,
    write_track_gain,
)
from melody.import_sources import get_import_sources
from melody.services.metadata import MetadataError
from melody.widgets.image_picker import show_image_picker
from melody.widgets.measuring_dialog import show_measuring_dialog
from melody.widgets.replaygain_analysis_dialog import (
    show_replaygain_analysis_dialog,
)
from melody.widgets.automatic_playlist_dialog import (
    show_automatic_playlist_dialog,
)
from melody.widgets.import_dialogs import (
    pick_audio_files,
    show_import_dialog,
    show_import_preview,
    show_link_dialog,
    show_search_dialog,
)
from melody.widgets.player_bar import PlayerBar
from melody.widgets.count_control import CountControl
from melody.widgets.equalizer_dialog import show_equalizer_dialog
from melody.widgets.track_table import (
    RECENT_COLUMNS,
    TrackSort,
    set_favorite_handler,
)


class MelodyWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Melody")

        self.set_default_size(1100, 720)

        self.tracks = []
        # The pages visited, to go back and forth between them with the
        # arrows of the sidebar, and where the current one is in that list.
        self._history = []
        self._history_index = -1
        # The sub-menu under Playlists in the sidebar: the categories it lists
        # now, and the page being shown (see ``_select_page``).
        self._playlist_categories = None
        self._current_page = ""
        self._sync_thread = None
        # True while a library scan is running; ``_rescan_done`` is told when
        # the scan a Preferences "Rescan" button started is over.
        self._syncing = False
        self._rescan_done = None
        # Downloads waiting in temporary folders until they have been
        # imported (see ``_discard_downloads``).
        self._downloads = []
        # The session (the page, the queue and where the track was) is picked
        # up after the first successful library scan, and only then saved
        # again when Melody closes (see ``_restore_session``).
        self._session_restored = False
        self.playback_manager = PlaybackManager()
        self.library_manager = LibraryManager(get_settings().music_folder)
        self.database = self.library_manager.database
        self.library_editor = LibraryEditor(
            self.library_manager.music_folder,
            self.database,
        )

        self.settings = get_settings()

        # The equalizer: what was chosen last time is applied now, and every
        # change after that is heard at once.
        self.equalizer = get_equalizer()
        self._apply_equalizer()
        self.equalizer.connect_changed(self._apply_equalizer)

        self.views = {
            # Looks through everything at once.
            "Search": SearchView(),
            # The track being played, shown large.
            "Currently playing": NowPlayingView(self.playback_manager),
            # What is playing and what comes after it.
            "Playing queue": QueueView(self.playback_manager),
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
            "Favorites": LibraryView(
                title="Favorites",
                noun="favorite",
                search_placeholder="Search favorites…",
                empty_title="No favorites yet",
                empty_description=(
                    "Click the heart next to a track to add it here."
                ),
                empty_icon="emblem-favorite-symbolic",
                track_filter=lambda track: bool(track.get("favorite")),
            ),
            "Recent": LibraryView(
                title="Recent",
                noun="track",
                search_placeholder="Search recent tracks…",
                empty_title="No recent tracks",
                empty_description=(
                    "Tracks you add to your library show up here."
                ),
                empty_icon="document-open-recent-symbolic",
                select_tracks=self._select_recent_tracks,
                columns=RECENT_COLUMNS,
                # Newest first, until a column name is clicked.
                initial_sort=TrackSort("added", True),
                # "Show the last N tracks added", set right in the header.
                header_widget=CountControl(
                    self.settings.recent_count,
                    self._set_recent_count,
                    before="Show the last",
                    after="tracks added",
                    tooltip=(
                        "How many of the most recently added tracks to show"
                    ),
                ),
            ),
            "Most played": LibraryView(
                title="Most played",
                noun="track",
                search_placeholder="Search most played tracks…",
                empty_title="No played tracks yet",
                empty_description=(
                    "Tracks you listen to show up here, the most played "
                    "first."
                ),
                empty_icon="audio-headphones-symbolic",
                select_tracks=self._select_most_played_tracks,
                # The most played first, until a column name is clicked.
                initial_sort=TrackSort("play_count", True),
                # "Show the N most played tracks", set right in the header.
                header_widget=CountControl(
                    self.settings.most_played_count,
                    self._set_most_played_count,
                    before="Show the",
                    after="most played tracks",
                    tooltip="How many of the most played tracks to show",
                ),
            ),
            "Albums": AlbumsView(),
            "Artists": ArtistsView(),
            "Playlists": PlaylistsView(),
        }

        for name in ("Library", "Compilations", "Favorites", "Recent", "Most played", "Albums", "Artists", "Search"):
            self.views[name].set_playback_manager(self.playback_manager)

        for name in ("Library", "Compilations", "Favorites", "Recent", "Most played", "Albums", "Artists", "Search"):
            self.views[name].set_context_actions(
                self._edit_track,
                self._choose_playlist_for_track,
            )
        self.views["Currently playing"].set_context_actions(
            self._edit_track,
            self._choose_playlist_for_track,
        )
        # The Library can import music.
        self.views["Library"].set_import_callback(self._import_music)
        # The pages of an album, an artist and a playlist can too, and what
        # they import starts out belonging to them.
        for name in ("Albums", "Artists", "Playlists"):
            self.views[name].set_import_callback(self._import_music)
        # Audio files can be dropped on those same pages, and only on those:
        # they are the ones with an Import button.
        for name in ("Library", "Albums", "Artists", "Playlists"):
            self.views[name].set_drop_callback(self._import_dropped_files)
        set_track_deletion(self._delete_track)
        # The heart next to a track in every list.
        set_favorite_handler(self._toggle_favorite)
        set_track_queue_actions(
            self.playback_manager.play_after_current,
            self.playback_manager.add_to_queue,
        )
        # The right-click menu of several selected tracks.
        set_tracks_actions(
            play=self._play_tracks,
            play_after=self.playback_manager.play_tracks_after_current,
            add_to_queue=self.playback_manager.add_tracks_to_queue,
            set_album=self._set_album_of_tracks,
            set_artist=self._set_artist_of_tracks,
            add_to_playlist=self._choose_playlist_for_tracks,
            favorite=self._toggle_favorite_of_tracks,
            delete=self._delete_tracks,
        )
        # "Copy artwork" / "Paste artwork" in the right-click menus.
        set_artwork_actions(
            "track", self._copy_track_artwork, self._paste_track_artwork,
        )
        set_artwork_actions(
            "album", self._copy_album_artwork, self._paste_album_artwork,
        )
        set_artwork_actions(
            "artist", self._copy_artist_image, self._paste_artist_image,
        )
        set_artwork_actions(
            "playlist",
            self._copy_playlist_artwork,
            self._paste_playlist_artwork,
        )
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
        # A search result opens the artist's or the album's page.
        self.views["Search"].set_navigation(
            self._open_artist_of_track,
            self._open_album_of_track,
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
        self.views["Playlists"].set_automatic_callback(
            self._create_automatic_playlist,
        )
        self.views["Playlists"].set_image_callback(
            self._change_playlist_image,
        )
        # The categories of the playlists are listed under Playlists in the
        # sidebar.
        self.views["Playlists"].set_categories_callback(
            self._update_playlist_categories,
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

        # Space, the arrows, M, S and R control the player (see
        # ``keyboard_shortcuts``). The player bar exists by now: it is made
        # with the content above.
        self.shortcuts = KeyboardShortcuts(
            self,
            self.playback_manager,
            self.player_bar,
        )

        # Music added, changed or removed by hand in the music folder is
        # noticed, and the library follows (see ``library_watcher``). It is
        # told when Melody works on the folder itself, which is not a change
        # to scan for.
        self.library_watcher = LibraryWatcher(
            self.library_manager.music_folder,
            self._on_music_folder_changed,
        )

        if self.settings.auto_scan_enabled:
            self.library_watcher.start()

        self._update_compilations_ui()
        self.settings.connect_changed(self._on_settings_changed)
        self._apply_transitions()

        # The categories already known are in the sidebar from the start,
        # before the first scan has filled the Playlists page.
        self._update_playlist_categories(
            self.database.get_playlist_categories()
        )

        self._select_page("Library")
        self._start_library_sync()

    def _create_sidebar(self):
        header_bar = Adw.HeaderBar()

        # Back at the left of the header bar and forward at the right, so the
        # title stays in the middle, balanced.
        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text("Back")
        self.back_button.set_sensitive(False)
        self.back_button.connect(
            "clicked",
            lambda _button: self._step_history(-1),
        )

        self.forward_button = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self.forward_button.set_tooltip_text("Forward")
        self.forward_button.set_sensitive(False)
        self.forward_button.connect(
            "clicked",
            lambda _button: self._step_history(1),
        )

        header_bar.pack_start(self.back_button)
        header_bar.pack_end(self.forward_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header_bar)

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
            # The first page: searches everything.
            ("Search", "system-search-symbolic"),
            ("Currently playing", "media-playback-start-symbolic"),
            ("Playing queue", "view-list-ordered-symbolic"),
            ("Library", "audio-x-generic-symbolic"),
            # Only shown while compilations are enabled.
            ("Compilations", "media-playlist-consecutive-symbolic"),
            ("Favorites", "emblem-favorite-symbolic"),
            ("Recent", "document-open-recent-symbolic"),
            ("Most played", "audio-headphones-symbolic"),
            ("Albums", "media-optical-symbolic"),
            ("Artists", "system-users-symbolic"),
            ("Playlists", "view-list-symbolic"),
        ]:
            row = self._create_navigation_row(name, icon)
            self.navigation_rows[name] = row
            self.navigation_list.append(row)

        # A line before Library, before Albums and before Playlists splits
        # the pages into four groups: Search, the tracks (Library,
        # Compilations, Favorites, Recent, Most played), the collections
        # (Albums, Artists) and the Playlists.
        group_starts = (
            self.navigation_rows["Library"],
            self.navigation_rows["Albums"],
            self.navigation_rows["Playlists"],
        )

        def update_header(row, _before):
            if row in group_starts:
                if row.get_header() is None:
                    separator = Gtk.Separator(
                        orientation=Gtk.Orientation.HORIZONTAL,
                    )
                    separator.set_margin_top(6)
                    separator.set_margin_bottom(6)
                    separator.set_margin_start(12)
                    separator.set_margin_end(12)
                    row.set_header(separator)
            else:
                row.set_header(None)

        self.navigation_list.set_header_func(update_header)

        sidebar.append(self.navigation_list)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        preferences_row = self._create_navigation_row(
            "Preferences",
            "emblem-system-symbolic",
        )
        equalizer_row = self._create_navigation_row(
            "Equalizer",
            "audio-speakers-symbolic",
        )

        # Not pages: they open in a window of their own.
        preferences_list = Gtk.ListBox()
        preferences_list.set_selection_mode(Gtk.SelectionMode.NONE)
        preferences_list.set_activate_on_single_click(True)
        preferences_list.add_css_class("navigation-sidebar")
        preferences_list.connect(
            "row-activated",
            lambda _list_box, row: (
                self._show_equalizer()
                if row is equalizer_row
                else self._show_preferences()
            ),
        )
        preferences_list.append(equalizer_row)
        preferences_list.append(preferences_row)
        sidebar.append(preferences_list)

        toolbar.set_content(sidebar)
        return Adw.NavigationPage.new(toolbar, "Navigation")

    @staticmethod
    def _create_navigation_row(title, icon, indent=False):
        row = Gtk.ListBoxRow()

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        # A row of the sub-menu under Playlists sits further in.
        content.set_margin_start(30 if indent else 12)
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
        self.playback_manager.on_play_counted = self._on_play_counted
        self.player_bar.on_play_idle = self._play_selected_track
        self.player_bar.on_view = self._view_track_source

        # Right-clicking the track shown in the bar opens the same menu as on
        # the track itself, plus "View": the page it was played from.
        attach_track_context_menu(
            self.player_bar.preview,
            lambda: self.playback_manager.current_track,
            on_play_pause=lambda _track: self.playback_manager.toggle_playback(),
            is_playing=lambda track: track_is_playing(
                self.playback_manager, track,
            ),
            on_edit=self._edit_track,
            on_add_to_playlist=self._choose_playlist_for_track,
            on_view=self._view_track_source,
            position=Gtk.PositionType.TOP,
        )

        # Media keys, the shell's media widget and playerctl can see and
        # control the player.
        self.mpris = MprisService(
            self.playback_manager,
            play=self.player_bar.press_play,
            raise_window=self.present,
            quit_app=lambda: GLib.idle_add(self.close),
            sync_options=self.player_bar.sync_options,
            set_volume=self.player_bar.set_volume,
        )
        self.mpris.start()

        layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        layout.append(self.content_stack)
        layout.append(self.sync_status)
        layout.append(self.player_bar)

        self.content_toolbar.set_content(layout)
        return Adw.NavigationPage.new(self.content_toolbar, "Content")

    def _on_play_counted(self, track):
        """A track has been listened to long enough: count one more play."""
        try:
            count = self.database.increment_play_count(track["id"])
        except Exception:
            return

        # The queue may hold its own copy of the track (a playlist's), so the
        # library's copy is updated too.
        track["play_count"] = count

        for candidate in self.tracks:
            if candidate.get("id") == track.get("id"):
                candidate["play_count"] = count

        # Every list showing the track shows its new count.
        for view in self.views.values():
            update = getattr(view, "update_play_count", None)

            if update is not None:
                update(track["id"], count)

    def _select_recent_tracks(self, tracks):
        """The tracks of the Recent page among ``tracks``: as many as the
        header's number says, newest first."""
        return select_recent_tracks(tracks, self.settings)

    def _set_recent_count(self, count):
        """A new number in the Recent page's header: save it, which also
        brings the pages up to date (see ``_on_settings_changed``)."""
        self.settings.recent_count = count

    def _select_most_played_tracks(self, tracks):
        """The tracks of the Most played page among ``tracks``: as many as the
        header's number says, the most played first."""
        return select_most_played_tracks(tracks, self.settings)

    def _set_most_played_count(self, count):
        """A new number in the Most played page's header (see above)."""
        self.settings.most_played_count = count

    def _toggle_favorite(self, track):
        """A click on a track's heart: mark it as a favorite, or take the mark
        away."""
        self._set_favorite(track, not track.get("favorite"))

    def _toggle_favorite_of_tracks(self, tracks):
        """Mark several tracks as favorites, or, when they all are already,
        take the mark away from all of them."""
        favorite = not all(track.get("favorite") for track in tracks)

        for track in tracks:
            if bool(track.get("favorite")) == favorite:
                continue

            if not self._set_favorite(track, favorite, refresh_page=False):
                break

        GLib.idle_add(self._refresh_favorites_page)

    def _set_favorite(self, track, favorite, refresh_page=True):
        """Mark ``track`` as a favorite, or take the mark away, and update
        everything that shows it. False when it could not be saved."""
        try:
            self.database.set_favorite(track["id"], favorite)
        except Exception as error:
            show_error_dialog(self, "Could not change the favorite", error)
            return False

        # Rows and queues may hold their own copies of the track, so the
        # library's copy and every row showing it are updated.
        track["favorite"] = 1 if favorite else 0

        for candidate in self.tracks:
            if candidate.get("id") == track.get("id"):
                candidate["favorite"] = 1 if favorite else 0

        for view in self.views.values():
            update = getattr(view, "update_favorite", None)

            if update is not None:
                update(track["id"], favorite)

        current = self.playback_manager.current_track

        if current is not None and current.get("id") == track.get("id"):
            current["favorite"] = 1 if favorite else 0

        # The Favorites page lists exactly the marked tracks. Done once the
        # click is over, as it may remove the very row that was clicked.
        if refresh_page:
            GLib.idle_add(self._refresh_favorites_page)

        return True

    def _refresh_favorites_page(self):
        self.views["Favorites"].set_tracks(self.tracks)
        return GLib.SOURCE_REMOVE

    def _show_preferences(self):
        show_preferences_dialog(
            self,
            self.settings,
            self._rescan_library,
            self._change_music_folder,
        )

    def _change_music_folder(self, folder, done=None):
        """Make ``folder`` the music folder, and scan it.

        Tracks whose file is in the new folder too, at the same place (the
        music was moved or copied there), stay the same tracks. The others
        leave the library when the scan finds them missing. ``done()`` is
        called when that scan is over.

        False (and nothing changed) when a scan is already running.
        """
        if self._syncing:
            return False

        folder = Path(folder).expanduser().resolve()

        try:
            self.database.rebase_paths(
                self.library_manager.music_folder,
                folder,
            )
        except Exception as error:
            show_error_dialog(self, "Could not change the music folder", error)
            return False

        self.library_manager.music_folder = folder
        self.library_editor.music_folder = folder
        self.library_watcher.set_folder(folder)

        # What was playing came from the old folder.
        self.playback_manager.stop()
        self.player_bar.set_track(None)

        self.settings.music_folder = folder
        self._rescan_library(done)
        return True

    def _apply_equalizer(self):
        """Give the player the equalizer's gains (all zeros while it is off)
        and whether to use ReplayGain."""
        self.playback_manager.set_equalizer(self.equalizer.effective_gains())
        self.playback_manager.set_replaygain_enabled(
            self.equalizer.replaygain_enabled
        )

    def _show_equalizer(self):
        show_equalizer_dialog(
            self,
            self.equalizer,
            available=self.playback_manager.has_equalizer,
            analyse=self._analyse_replaygain,
        )

    # ------------------------------------------------------------------
    # ReplayGain analysis
    # ------------------------------------------------------------------

    def _analyse_replaygain(self, parent):
        """The "Analyse…" button of the equalizer window: choose what to
        analyse, then measure it and set the ReplayGain of those tracks."""
        if not analysis_available():
            show_error_dialog(
                parent,
                "Analysis unavailable",
                Exception(
                    "GStreamer's ReplayGain analysis plugin (rganalysis) was "
                    "not found. It is part of gst-plugins-good: install that "
                    "package, then restart Melody."
                ),
            )
            return

        # The files are rewritten, which a scan running at the same time
        # would trip over.
        if self._syncing:
            show_error_dialog(
                parent,
                "The library is busy",
                Exception(
                    "Wait for the library scan to finish, then try again."
                ),
            )
            return

        show_replaygain_analysis_dialog(
            parent,
            self._analysis_scopes(),
            self._run_analysis,
        )

    def _analysis_scopes(self):
        """What the analysis window offers, in order. Each one gives the tracks
        when asked (a scope with ``choices`` lists what to pick from first)."""
        scopes = [
            {"title": "All tracks", "tracks": lambda: list(self.tracks)},
        ]

        # With compilations off there is no difference between "all" and
        # "library", nor any compilation to speak of.
        if self.settings.compilations_enabled:
            scopes.append({
                "title": "Library",
                "tracks": lambda: [
                    track for track in self.tracks
                    if not is_compilation(track)
                ],
            })
            scopes.append({
                "title": "Compilations",
                "tracks": lambda: [
                    track for track in self.tracks if is_compilation(track)
                ],
            })

        scopes.extend([
            {
                "title": "Favorites",
                "tracks": lambda: [
                    track for track in self.tracks if track.get("favorite")
                ],
            },
            {
                "title": "Recent",
                "tracks": lambda: self._select_recent_tracks(self.tracks),
            },
            {
                "title": "Most played",
                "tracks": lambda: self._select_most_played_tracks(
                    self.tracks
                ),
            },
            {
                "title": "Albums",
                "choices": self._album_choices,
                "search_placeholder": "Search albums…",
            },
            {
                "title": "Artists",
                "choices": self._artist_choices,
                "search_placeholder": "Search artists…",
            },
            {
                "title": "Playlists",
                "choices": self._playlist_choices,
                "search_placeholder": "Search playlists…",
            },
        ])

        return scopes

    @staticmethod
    def _count_text(count):
        return f"{count} track{'s' if count != 1 else ''}"

    def _album_choices(self):
        """Every album, A-Z, with its tracks."""
        albums = {}

        for track in self.tracks:
            title = str(track.get("album") or "").strip()

            if not title:
                continue

            artist = artist_name(track)
            entry = albums.setdefault(
                (artist.casefold(), title.casefold()),
                {"title": title, "artist": artist, "tracks": []},
            )
            entry["tracks"].append(track)

        return [
            {
                "title": entry["title"],
                "subtitle": (
                    f"{entry['artist']} · {self._count_text(len(entry['tracks']))}"
                ),
                "tracks": entry["tracks"],
            }
            for entry in sorted(
                albums.values(),
                key=lambda entry: (
                    entry["title"].casefold(),
                    entry["artist"].casefold(),
                ),
            )
        ]

    def _artist_choices(self):
        """Every artist, A-Z, with all their tracks."""
        artists = {}

        for track in self.tracks:
            name = artist_name(track)
            entry = artists.setdefault(
                name.casefold(),
                {"title": name, "tracks": []},
            )
            entry["tracks"].append(track)

        return [
            {
                "title": entry["title"],
                "subtitle": self._count_text(len(entry["tracks"])),
                "tracks": entry["tracks"],
            }
            for entry in sorted(
                artists.values(),
                key=lambda entry: entry["title"].casefold(),
            )
        ]

    def _playlist_choices(self):
        """Every playlist, A-Z (its tracks are only looked up if picked)."""
        playlists_view = self.views["Playlists"]

        return [
            {
                "title": str(playlist.get("name") or ""),
                "subtitle": self._count_text(playlist.get("track_count", 0)),
                "tracks": (
                    lambda playlist=playlist:
                    playlists_view.library_tracks_of(playlist)
                ),
            }
            for playlist in sorted(
                self.database.get_playlists(),
                key=lambda playlist: str(playlist.get("name") or "").casefold(),
            )
        ]

    def _run_analysis(self, tracks, skip_tagged, progress, should_stop):
        """Analyse ``tracks`` (called in a thread of its own by the analysis
        window), then let the player pick up a changed tag."""
        # Tagging hundreds of files is not a change to scan for.
        GLib.idle_add(self._pause_library_watcher)

        try:
            summary = analyse_tracks(
                tracks,
                skip_tagged=skip_tagged,
                progress=progress,
                should_stop=should_stop,
            )
        finally:
            GLib.idle_add(self._resume_library_watcher)

        # The track being played may have been tagged just now.
        GLib.idle_add(self._refresh_replaygain_once)
        return summary

    def _pause_library_watcher(self):
        self.library_watcher.pause()
        return GLib.SOURCE_REMOVE

    def _resume_library_watcher(self):
        self.library_watcher.ignore_for(2.0)
        self.library_watcher.resume()
        return GLib.SOURCE_REMOVE

    def _refresh_replaygain_once(self):
        self.playback_manager.refresh_replaygain()
        return GLib.SOURCE_REMOVE

    def _update_compilations_ui(self):
        """The Compilations page only exists while compilations are enabled."""
        enabled = self.settings.compilations_enabled
        self.navigation_rows["Compilations"].set_visible(enabled)

        if (
                not enabled
                and self.content_stack.get_visible_child_name() == "compilations"
        ):
            self._select_page("Library")

    def _apply_transitions(self):
        """Give the player how one track follows the next (see
        ``PlaybackManager.set_transitions``)."""
        self.playback_manager.set_transitions(
            self.settings.gapless_enabled,
            self.settings.crossfade_seconds
            if self.settings.crossfade_enabled else 0.0,
        )

    def _on_settings_changed(self):
        # Watching the folder can be switched on and off.
        if self.settings.auto_scan_enabled:
            self.library_watcher.start()
        else:
            self.library_watcher.stop()

        # Which tracks go where depends on the settings: sort them out again.
        self._update_compilations_ui()

        self._apply_transitions()

        for name in ("Library", "Compilations", "Favorites", "Recent", "Most played", "Albums", "Artists", "Search"):
            self.views[name].set_tracks(self.tracks)

    def _update_playlist_categories(self, categories):
        """The sub-menu under Playlists: one row for each category of
        playlists, A-Z. The page of a category lists the playlists filed
        under it (see ``_select_page``)."""
        categories = list(categories)

        if categories == self._playlist_categories:
            return

        self._playlist_categories = categories

        for name in [
            name for name in self.navigation_rows
            if name.startswith("Playlists/")
        ]:
            self.navigation_list.remove(self.navigation_rows.pop(name))

        index = self.navigation_rows["Playlists"].get_index() + 1

        for category in categories:
            row = self._create_navigation_row(
                category,
                "folder-symbolic",
                indent=True,
            )
            self.navigation_rows[f"Playlists/{category}"] = row
            self.navigation_list.insert(row, index)
            index += 1

        current = self._current_page

        if current.startswith("Playlists/") and (
                current not in self.navigation_rows
        ):
            # The category being shown has no playlist left.
            self._select_page("Playlists")
        elif current in self.navigation_rows:
            # Taking the rows out may have cleared the selection.
            self.navigation_list.select_row(self.navigation_rows[current])

    def _playlist_page(self, playlist):
        """The page of the sidebar that lists ``playlist``: Playlists, or its
        category's."""
        category = clean_category(playlist.get("category"))
        return f"Playlists/{category}" if category else "Playlists"

    def _on_navigation_selected(self, list_box, row):
        if row is None:
            return

        for name, navigation_row in self.navigation_rows.items():
            if navigation_row is row:
                self._select_page(name)
                break

    def _record_page(self, name):
        """Remember that ``name`` was shown, for the back and forward arrows.
        Going somewhere new drops the pages that were ahead."""
        if (
                self._history_index >= 0
                and self._history[self._history_index] == name
        ):
            return

        del self._history[self._history_index + 1:]
        self._history.append(name)
        self._history_index = len(self._history) - 1
        self._update_history_buttons()

    def _update_history_buttons(self):
        self.back_button.set_sensitive(self._history_index > 0)
        self.forward_button.set_sensitive(
            self._history_index < len(self._history) - 1
        )

    def _step_history(self, step):
        """Go to the previous (-1) or next (1) page visited, skipping a page
        that is not there any more (Compilations, once turned off)."""
        index = self._history_index + step

        while 0 <= index < len(self._history):
            row = self.navigation_rows.get(self._history[index])

            if row is not None and row.get_visible():
                self._history_index = index
                self._select_page(self._history[index])
                self._update_history_buttons()
                return

            index += step

    def _select_page(self, name):
        self._record_page(name)
        self._current_page = name

        # A category of playlists is a page of the sidebar of its own, named
        # "Playlists/<category>": the Playlists view lists its playlists.
        page, _, category = name.partition("/")

        self.content_stack.set_visible_child_name(page.lower())
        self.content_header.set_title_widget(
            Gtk.Label(label=category or page)
        )

        if page == "Playlists":
            self.views["Playlists"].set_category(category or None)

            # Plays, favorites and imports since the page was last shown may
            # have changed what the automatic playlists select.
            self.views["Playlists"].refresh_automatic()

        if name == "Most played":
            # Plays since the page was last shown may have changed the
            # ranking.
            self.views["Most played"].set_tracks(self.tracks)

        if name == "Search":
            # Ready to type.
            self.views["Search"].focus_search()

        if name in self.navigation_rows:
            self.navigation_list.select_row(
                self.navigation_rows[name]
            )

    def _start_library_sync(self):
        self._syncing = True
        # The scan moves files and writes covers: those changes are its own.
        self.library_watcher.pause()
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
        self.library_watcher.resume()

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
            report = self.library_manager.sync(
                organize=self.settings.organize_files,
            )
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
        self.playback_manager.refresh_tracks(tracks)
        self._refresh_player_bar()

        self.views["Library"].set_tracks(tracks)
        self.views["Compilations"].set_tracks(tracks)
        self.views["Favorites"].set_tracks(tracks)
        self.views["Recent"].set_tracks(tracks)
        self.views["Most played"].set_tracks(tracks)
        self.views["Search"].set_tracks(tracks)
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

        if not self._session_restored:
            self._session_restored = True
            self._restore_session()

        self._finish_sync()
        return GLib.SOURCE_REMOVE

    def _add_track_to_playlist(self, track, playlist_id):
        try:
            self.database.add_track_to_playlist(playlist_id, track["id"])
            self.views["Playlists"].refresh()
        except Exception as error:
            show_error_dialog(self, "Could not add track", error)

    def _manual_playlists(self, exclude_playlist_id=None):
        # An automatic playlist takes no track by hand (its rules pick them),
        # so it is not offered.
        return [
            playlist for playlist in self.database.get_playlists()
            if playlist["id"] != exclude_playlist_id
            and not playlist.get("automatic")
        ]

    def _choose_playlist_for_track(self, track, exclude_playlist_id=None):
        playlists = self._manual_playlists(exclude_playlist_id)
        show_playlist_picker_dialog(
            self,
            track,
            playlists,
            lambda playlist_id: self._add_track_to_playlist(track, playlist_id),
            lambda: self._create_playlist_for_track(track),
        )

    def _add_tracks_to_playlist(self, tracks, playlist_id):
        try:
            self.database.add_tracks_to_playlist(
                playlist_id,
                [track["id"] for track in tracks],
            )
            self.views["Playlists"].refresh()
        except Exception as error:
            show_error_dialog(self, "Could not add tracks", error)

    def _choose_playlist_for_tracks(self, tracks, exclude_playlist_id=None):
        """"Add to playlist…" for several selected tracks."""
        playlists = self._manual_playlists(exclude_playlist_id)
        show_playlist_picker_dialog(
            self,
            tracks,
            playlists,
            lambda playlist_id: self._add_tracks_to_playlist(
                tracks, playlist_id,
            ),
            lambda: self._create_playlist_for_tracks(tracks),
        )

    def _create_playlist_for_tracks(self, tracks):
        def create(name):
            try:
                playlist_id = self.database.create_playlist(name)
                self.database.add_tracks_to_playlist(
                    playlist_id,
                    [track["id"] for track in tracks],
                )
                self.views["Playlists"].refresh()
            except Exception as error:
                show_error_dialog(self, "Could not create playlist", error)

        show_text_dialog(self, "Create playlist", "Choose a name for the new playlist.", "", create)

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

    # ------------------------------------------------------------------
    # "View" in the player bar's track menu
    # ------------------------------------------------------------------

    def _view_track_source(self, track):
        """Show the page the playing queue was started from (a playlist, an
        album, an artist, the Library or the Compilations page) and scroll to
        the track there when that page lists it."""
        source = self.playback_manager.source
        kind = source["kind"] if source else None
        ids = set(source["ids"]) if source else set()
        page = None
        shown = False

        if kind == "playlist":
            playlist = self.database.get_playlist(source["key"])
            # A playlist in a category is on that category's page.
            page = self._playlist_page(playlist) if playlist else "Playlists"
            # Shown first: showing the page refreshes the automatic playlists,
            # which would rebuild the rows the view is about to scroll to.
            self._select_page(page)
            shown = self.views["Playlists"].view_playlist(
                source["key"], track,
            )
        elif kind == "album":
            page = "Albums"
            shown = self.views[page].view_album(ids, track)
        elif kind in ("artist", "artist-compilations"):
            page = "Artists"
            shown = self.views[page].view_artist(ids, track)
        elif kind == "list":
            if source["key"] == self.views["Favorites"].title:
                page = "Favorites"
            elif source["key"] == self.views["Recent"].title:
                page = "Recent"
            elif source["key"] == self.views["Most played"].title:
                page = "Most played"
            else:
                page = (
                    "Compilations"
                    if (
                        source["key"] == self.views["Compilations"].title
                        and self.settings.compilations_enabled
                    )
                    else "Library"
                )

            self.views[page].show_track(track)
            shown = True

        if not shown:
            # A track played on its own, or from something that no longer
            # exists: it is in the Library (or the Compilations page).
            page = (
                "Compilations"
                if self.settings.compilations_enabled and is_compilation(track)
                else "Library"
            )
            self.views[page].show_track(track)

        self._select_page(page)

    def _edit_track(self, track):
        def save(values):
            image = values.get("image")
            changed = (
                any(
                    values[key].strip() != str(track.get(key) or "").strip()
                    for key in ("title", "artist", "album", "year")
                )
                or not same_genres(values["genre"], track.get("genre"))
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

            if "replaygain" in values and not self._save_replaygain(
                    track, values["replaygain"],
            ):
                return

            if changed:
                try:
                    result = self.library_editor.edit_track(
                        track["id"],
                        values["title"],
                        values["artist"],
                        values["album"],
                        year=values["year"].strip(),
                        genre=values["genre"],
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
            self,
            track,
            save,
            library_tracks=self.tracks,
            # The ReplayGain field only shows while ReplayGain is switched on.
            replaygain=self.equalizer.replaygain_enabled,
        )

    def _save_replaygain(self, track, gain):
        """Write the ReplayGain chosen in the edit window (a number of dB, or
        None to take the tag away) into the track's file. Done before the
        rest of the edit, so a rename carries it along. False, after telling
        the person, when it could not be saved."""
        try:
            # Nothing to write when it is what the file already has.
            if gain == read_track_gain(track["path"]):
                return True

            write_track_gain(track["path"], gain)
        except ReplayGainError as error:
            show_error_dialog(self, "Could not save the ReplayGain", error)
            return False

        # If it is the track being played, it sounds right away.
        self.playback_manager.refresh_replaygain()
        return True

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
    # Several selected tracks (their right-click menu)
    # ------------------------------------------------------------------

    def _play_tracks(self, tracks):
        """Play the selected tracks, in the order shown, as a queue of their
        own."""
        self.playback_manager.play_queue(tracks, 0)

    def _set_album_of_tracks(self, tracks):
        """"Set album…": ask for an album (and its album artist), then give it
        to every selected track."""
        if self._syncing:
            self._show_library_busy()
            return

        show_set_album_dialog(
            self,
            tracks,
            self.tracks,
            lambda values: self._edit_tracks(
                tracks,
                album=values["album"],
                album_artist=values["artist"],
            ),
        )

    def _set_artist_of_tracks(self, tracks):
        """"Set artist…": ask for an artist, then give it to every selected
        track."""
        if self._syncing:
            self._show_library_busy()
            return

        show_set_artist_dialog(
            self,
            tracks,
            self.tracks,
            lambda values: self._edit_tracks(
                tracks,
                artist=values["artist"],
            ),
        )

    def _edit_tracks(self, tracks, **changes):
        """Give every one of ``tracks`` the album and / or artist in
        ``changes`` (see ``LibraryEditor.edit_tracks``)."""
        # The files are rewritten and moved, which a scan running at the same
        # time would undo.
        if self._syncing:
            self._show_library_busy()
            return

        try:
            result = self.library_editor.edit_tracks(
                [track["id"] for track in tracks],
                **changes,
            )
        except Exception as error:
            show_error_dialog(self, "Could not edit the tracks", error)
            return

        self._finish_collection_rename(result, "track")

        if result.get("moved"):
            # The tracks are in other albums now: their covers are looked up
            # again by a scan in the background (it may need the network,
            # which must not freeze the window).
            self._rescan_library()

    def _delete_tracks(self, tracks):
        """The "Delete" entry of the menu of several selected tracks: ask, then
        move their files to the trash and take them out of the library and
        their playlists."""
        if self._syncing:
            self._show_library_busy()
            return

        count = len(tracks)

        dialog = Adw.MessageDialog.new(
            self,
            f"Delete {count} tracks?",
            f"These {count} tracks will be moved to the trash and removed "
            "from your library and from their playlists.",
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
            lambda _dialog, response: self._on_delete_tracks_response(
                tracks, response,
            ),
        )
        dialog.present()

    def _on_delete_tracks_response(self, tracks, response):
        if response != "delete":
            return

        deleted = set()
        errors = []

        for track in tracks:
            try:
                result = self.library_editor.delete_track(track["id"])
            except Exception as error:
                name = track.get("title") or track.get("path")
                errors.append(f"{name}: {error}")
                continue

            deleted.add(track["id"])
            errors.extend(result.get("errors", []))

        # A deleted track must not keep playing.
        current = self.playback_manager.current_track

        if current is not None and current.get("id") in deleted:
            self.playback_manager.stop()
            self.player_bar.set_track(None)

        if errors:
            show_error_dialog(
                self,
                (
                    "Some tracks could not be deleted"
                    if len(deleted) < len(tracks)
                    else (
                        "The tracks were deleted, but not everything was "
                        "cleaned up"
                    )
                ),
                "\n".join(errors),
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

    def _import_dropped_files(self, paths, context=None):
        """Audio files were dropped on a page that can import music.

        This is the Import button, then "From file", then choosing these
        files and clicking Open, without the two windows in between: what
        comes next is the same (the edit window for one file, the preview
        for several), with the same ``context`` (the album, artist or
        playlist of the page the files were dropped on).
        """
        # Importing writes to the library and the database, which a scan
        # running at the same time would undo.
        if self._syncing:
            self._show_library_busy()
            return

        source = next(
            (
                candidate for candidate in get_import_sources()
                if candidate.kind == "files"
            ),
            None,
        )

        if source is None:
            return

        self._on_import_files_picked(source, paths, dict(context or {}))

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

        # With ReplayGain on, each track's loudness is measured first, so
        # its edit window starts from a ReplayGain that evens it out.
        self._measure_pending(
            pending,
            lambda: self._show_pending_review(pending, errors, context),
        )

    def _measure_pending(self, pending, then):
        """Measure the loudness of the tracks about to be imported, when
        ReplayGain is on, then call ``then()``.

        Each measured track gets its ReplayGain set from it (the person can
        still change it in the edit window). A track that is already tagged
        keeps its own, and one in a format that cannot hold the tag (Opus) is
        left alone. Without the analysis plugin nothing is measured.
        """
        if not self.equalizer.replaygain_enabled or not analysis_available():
            then()
            return

        todo = [
            item for item in pending
            if supports_replaygain(item.path)
            and read_track_gain(item.path) is None
        ]

        if not todo:
            then()
            return

        def run(progress, should_stop):
            return measure_files(
                [item.path for item in todo],
                progress=progress,
                should_stop=should_stop,
            )

        def done(measured):
            for item in todo:
                found = measured.get(item.path)

                if found is not None:
                    item.replaygain, item.replaygain_peak = found

            then()

        show_measuring_dialog(self, len(todo), run, done)

    def _show_pending_review(self, pending, errors, context):
        """The windows that show what is about to be imported (see
        ``_review_pending_tracks``)."""
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
                replaygain=self.equalizer.replaygain_enabled,
            )
            return

        # Several files: a preview of them all, each editable, then Save adds
        # every one of them.
        show_import_preview(
            self,
            pending,
            self.tracks,
            lambda items: self._import_tracks(items, errors, playlist_id),
            replaygain=self.equalizer.replaygain_enabled,
        )

    def _import_tracks(self, pending, earlier_errors=(), playlist_id=None):
        """Import the pending files: the last step of every way of importing
        (files, a dropped file, a link, a search).

        Files that look like a track the library already has (or like another
        file of this same import) are asked about first, since importing one
        would add the song a second time. See ``_copy_into_library`` for the
        arguments.
        """
        if self._syncing:
            self._show_library_busy()
            return

        duplicates = find_duplicates(pending, self.tracks)

        if duplicates:
            self._ask_about_duplicates(
                pending,
                duplicates,
                earlier_errors,
                playlist_id,
            )
            return

        self._copy_into_library(pending, earlier_errors, playlist_id)

    @staticmethod
    def _describe_duplicate(duplicate):
        """One line saying which file looks like a duplicate, and of what."""
        item = duplicate.item
        name = f"“{item.title}”"

        if item.artist:
            name += f" by {item.artist}"

        if duplicate.existing is None:
            return f"{name} (also in this import)"

        album = str(duplicate.existing.get("album") or "").strip()

        if album:
            return f"{name} (already in your library, on “{album}”)"

        return f"{name} (already in your library)"

    def _ask_about_duplicates(
            self,
            pending,
            duplicates,
            earlier_errors,
            playlist_id,
    ):
        """Tell the person which files look like tracks they already have, and
        let them skip those, import everything anyway, or give up."""
        duplicate_ids = {id(duplicate.item) for duplicate in duplicates}
        others = [item for item in pending if id(item) not in duplicate_ids]

        shown = 8
        lines = [
            self._describe_duplicate(duplicate)
            for duplicate in duplicates[:shown]
        ]

        if len(duplicates) > shown:
            lines.append(f"and {len(duplicates) - shown} more")

        if len(pending) == 1:
            heading = "Already in your library"
            body = (
                f"{lines[0]}.\n\n"
                "Importing it anyway adds a second copy to your library."
            )
        else:
            heading = "Possible duplicates"
            body = (
                "These tracks look like ones you already have:\n\n"
                + "\n".join(lines)
                + "\n\nImporting them anyway adds a second copy of each."
            )

        dialog = Adw.MessageDialog.new(self, heading, body)
        dialog.add_response("cancel", "Cancel")
        dialog.set_close_response("cancel")

        if others:
            # Some files are new: the safe choice is to import only those.
            dialog.add_response("import", "Import all")
            dialog.add_response("skip", "Skip duplicates")
            dialog.set_response_appearance(
                "skip",
                Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("skip")
        else:
            dialog.add_response("import", "Import anyway")
            dialog.set_default_response("cancel")

        dialog.connect(
            "response",
            lambda _dialog, response: self._on_duplicates_response(
                response,
                pending,
                others,
                earlier_errors,
                playlist_id,
            ),
        )
        dialog.present()

    def _on_duplicates_response(
            self,
            response,
            pending,
            others,
            earlier_errors,
            playlist_id,
    ):
        if response == "import":
            self._copy_into_library(pending, earlier_errors, playlist_id)
        elif response == "skip":
            self._copy_into_library(others, earlier_errors, playlist_id)
        else:
            # Nothing is imported: a download waiting in its temporary
            # folder is not needed any more.
            self._discard_downloads()

    def _copy_into_library(self, pending, earlier_errors=(), playlist_id=None):
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
                    replaygain=item.replaygain,
                    replaygain_peak=item.replaygain_peak,
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

    def _create_playlist(self, category=None):
        """A new playlist, its tracks picked by hand. ``category`` is the one
        the page it is made from lists, which the category field starts with.
        """
        def create(values):
            try:
                playlist_id = self.database.create_playlist(
                    values["name"],
                    values["category"] or None,
                )
            except Exception as error:
                show_error_dialog(self, "Could not create playlist", error)
                return

            self._open_playlist_page(playlist_id)

        show_new_playlist_dialog(
            self,
            self.database.get_playlist_categories(),
            category,
            create,
        )

    def _open_playlist_page(self, playlist_id):
        """Show a playlist's page, within the page of the sidebar that lists
        it (Playlists, or its category's)."""
        view = self.views["Playlists"]
        # First, so the sidebar has the playlist's category by then.
        view.refresh()
        playlist = self.database.get_playlist(playlist_id)

        if playlist is None:
            return

        self._select_page(self._playlist_page(playlist))
        view._show_playlist(playlist)

    def _finish_playlist_edit(self, playlist_id, moved):
        """Bring the Playlists page up to date after an edit. When the edit
        changed the playlist's category (``moved``), go to the page of its new
        category, with the playlist open if it was."""
        view = self.views["Playlists"]
        reopen = view.is_open(playlist_id)
        view.refresh()

        if not moved:
            return

        playlist = self.database.get_playlist(playlist_id)

        if playlist is None:
            return

        self._select_page(self._playlist_page(playlist))

        if reopen:
            view._show_playlist(playlist)

    def _create_automatic_playlist(self, category=None):
        def save(name, rules, chosen_category):
            try:
                playlist_id = self.database.create_automatic_playlist(
                    name, rules, chosen_category,
                )
            except Exception as error:
                show_error_dialog(self, "Could not create playlist", error)
                return

            self._open_playlist_page(playlist_id)

        show_automatic_playlist_dialog(
            self,
            None,
            self.tracks,
            save,
            categories=self.database.get_playlist_categories(),
            default_category=category,
        )

    def _edit_automatic_playlist(self, playlist):
        """Change the name and the rules of an automatic playlist. Its picture
        is changed like any playlist's, with the edit button over its cover."""
        def save(name, rules, category):
            moved = not same_category(category, playlist.get("category"))

            try:
                if name != playlist["name"]:
                    self.database.rename_playlist(playlist["id"], name)

                self.database.set_playlist_rules(playlist["id"], rules)

                if moved:
                    self.database.set_playlist_category(
                        playlist["id"], category,
                    )
            except Exception as error:
                show_error_dialog(self, "Could not edit playlist", error)
                return

            self._finish_playlist_edit(playlist["id"], moved)

        show_automatic_playlist_dialog(
            self,
            playlist,
            self.tracks,
            save,
            categories=self.database.get_playlist_categories(),
        )

    def _edit_playlist(self, playlist):
        if playlist.get("automatic"):
            self._edit_automatic_playlist(playlist)
            return

        def save(values):
            image = values.get("image")
            moved = not same_category(
                values["category"], playlist.get("category"),
            )

            try:
                if values["name"] != playlist["name"]:
                    self.database.rename_playlist(
                        playlist["id"], values["name"],
                    )

                if moved:
                    self.database.set_playlist_category(
                        playlist["id"], values["category"],
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

            self._finish_playlist_edit(playlist["id"], moved)

        show_edit_playlist_dialog(
            self,
            playlist,
            self.views["Playlists"].cover_for(playlist),
            save,
            categories=self.database.get_playlist_categories(),
        )

    def _change_playlist_image(self, playlist):
        """The edit button over a playlist's picture: pick, then apply right
        away (there is no Save step outside the edit window). A playlist's
        name says nothing about what it should look like, so the person
        searches YouTube for what they want, or picks an image file."""
        show_image_picker(
            self,
            playlist_search(),
            lambda data: self._apply_playlist_image(playlist, data),
        )

    def _apply_playlist_image(self, playlist, data):
        try:
            set_playlist_cover(self.database, playlist["id"], data)
        except Exception as error:
            show_error_dialog(
                self, "Could not change the playlist's image", error,
            )
            return

        self.views["Playlists"].refresh()

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

        show_image_picker(
            self,
            album_search(album["artist"], album["title"]),
            lambda data: self._apply_album_image(album, data),
        )

    def _apply_album_image(self, album, data):
        """Make ``data`` the album's cover (the tracks keep their own images)."""
        track_ids = [track["id"] for track in album["tracks"]]
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

    def _change_artist_image(self, artist):
        """The edit button over an artist's picture (see above)."""
        show_image_picker(
            self,
            artist_search(artist["name"]),
            lambda data: self._apply_artist_image(artist, data),
        )

    def _apply_artist_image(self, artist, data):
        result = {"errors": []}
        self._apply_image(
            result,
            lambda: self.library_editor.set_artist_image(
                artist["name"], data,
            ),
        )
        self._finish_collection_rename(result, "artist")

    def _apply_track_image(self, track, data):
        result = {"errors": []}
        self._apply_image(
            result,
            lambda: self.library_editor.set_track_image(track["id"], data),
        )
        self._finish_collection_rename(
            result, "track", refresh_artwork_ids=[track["id"]],
        )

    # ------------------------------------------------------------------
    # "Copy artwork" / "Paste artwork" in the right-click menus
    # ------------------------------------------------------------------

    def _copy_artwork(self, data, what):
        """Put ``data`` on the clipboard; ``what`` names the item for the
        message shown when there is nothing to copy."""
        if not data:
            show_error_dialog(
                self,
                "Nothing to copy",
                f"{what} has no image of its own to copy.",
            )
            return

        try:
            copy_image_to_clipboard(data)
        except Exception as error:
            show_error_dialog(self, "Could not copy the image", error)

    def _paste_artwork(self, apply):
        """Read the image on the clipboard and give it to ``apply(data)``."""
        def received(data):
            if not data:
                show_error_dialog(
                    self,
                    "Nothing to paste",
                    "The clipboard does not hold an image that can be read.",
                )
                return

            apply(data)

        read_clipboard_image(received)

    def _copy_track_artwork(self, track):
        try:
            data = get_track_artwork_bytes(Path(track["path"]))
        except Exception:
            data = None

        self._copy_artwork(data, f"“{track.get('title') or 'This track'}”")

    def _paste_track_artwork(self, track):
        self._paste_artwork(lambda data: self._apply_track_image(track, data))

    def _copy_album_artwork(self, album):
        self._copy_artwork(
            self._album_artwork_bytes(album),
            f"“{album['title']}”",
        )

    def _paste_album_artwork(self, album):
        self._paste_artwork(lambda data: self._apply_album_image(album, data))

    def _copy_artist_image(self, artist):
        folder = self.library_manager.music_folder / _sanitize_name(
            artist["name"]
        )
        self._copy_artwork(
            get_local_artist_artwork_bytes(folder),
            artist["name"],
        )

    def _paste_artist_image(self, artist):
        self._paste_artwork(lambda data: self._apply_artist_image(artist, data))

    def _copy_playlist_artwork(self, playlist):
        data = None
        chosen = get_playlist_cover(self.database, playlist["id"])

        if chosen:
            try:
                data = Path(chosen).read_bytes()
            except OSError:
                data = None

        if data is None:
            # No picture of its own: the cover of its first track, which is
            # what the playlist shows.
            first = self.views["Playlists"].library_tracks_of(playlist)[:1]

            for track in first:
                try:
                    data = get_album_artwork_bytes(Path(track["path"]))
                except Exception:
                    data = None

        self._copy_artwork(data, f"“{playlist['name']}”")

    def _paste_playlist_artwork(self, playlist):
        self._paste_artwork(
            lambda data: self._apply_playlist_image(playlist, data)
        )

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

    def _on_music_folder_changed(self):
        """Something other than Melody added, changed or removed files in the
        music folder: scan again, so the library (and the pages) follow."""
        if not self.settings.auto_scan_enabled or self._syncing:
            return

        self._rescan_library()

    def _reload_library_from_database(self, refresh_artwork_ids=()):
        # Whatever Melody did to the files just before (an edit, an import, a
        # deletion) is not a change to scan for.
        self.library_watcher.ignore_for(2.0)
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
        self.playback_manager.refresh_tracks(tracks)
        self._refresh_player_bar()
        self.views["Library"].set_tracks(tracks)
        self.views["Compilations"].set_tracks(tracks)
        self.views["Favorites"].set_tracks(tracks)
        self.views["Recent"].set_tracks(tracks)
        self.views["Most played"].set_tracks(tracks)
        self.views["Search"].set_tracks(tracks)
        self.views["Albums"].set_tracks(tracks)
        self.views["Artists"].set_tracks(tracks)
        self.views["Playlists"].set_data(self.database, tracks, self.playback_manager)

    def _refresh_player_bar(self):
        """After the library was reloaded, show the playing track as it is now
        (an edit may have changed its title, artist or artwork)."""
        current = self.playback_manager.current_track

        if current is not None:
            self.player_bar.refresh_track(current)

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
        """Play button with nothing loaded: do what the page showing would do.

        The selected track is played first, like a double click on it (the
        Library and Compilations pages). With none selected, it is the page's
        own Play button: the whole list on the Library and Compilations
        pages, the album, artist or playlist whose page is open.

        False when the page has nothing to play (a grid of albums, say), so
        the button does what it normally does.
        """
        page = self.content_stack.get_visible_child_name()
        view = self.views.get(page.capitalize())

        if view is None:
            return False

        play_selected = getattr(view, "play_selected", None)

        if play_selected is not None and play_selected():
            return True

        return view.play_page()

    def _on_playback_error(self, message):
        self.sync_status.set_visible(True)
        self.sync_status_label.set_text(
            f"Playback error: {message}"
        )

    def _hide_sync_status(self):
        self.sync_status.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _restore_session(self):
        """Pick up where the last session was left: the page that was shown,
        and the queue with the track that was playing, loaded and paused at the
        position it had (Play goes on from there). Tracks no longer in the
        library are left out of the queue."""
        session = load_session()

        if not session:
            return

        page = session.get("page")

        # A page that is gone (a playlist category with no playlist left, or
        # Compilations once turned off) falls back to the Library.
        row = self.navigation_rows.get(page)

        if row is None or not row.get_visible():
            page = "Library"

        library = {track.get("id"): track for track in self.tracks}
        saved_index = session.get("index", -1)
        queue = []
        index = -1

        for slot, track_id in enumerate(session.get("queue") or []):
            track = library.get(track_id)

            if track is None:
                continue

            if slot == saved_index:
                index = len(queue)

            queue.append(track)

        if index >= 0:
            try:
                position = float(session.get("position") or 0.0)
            except (TypeError, ValueError):
                position = 0.0

            self.playback_manager.resume(
                queue,
                index,
                position=position,
                source=self._source_from_session(session.get("source")),
            )

        # The page the session starts on is the first one in the history, so
        # the back arrow does not lead to the Library page of the launch.
        self._history = []
        self._history_index = -1
        self._select_page(page)

    @staticmethod
    def _source_from_session(saved):
        """What the saved queue was started from (see ``playback_source``),
        read back from the session file."""
        if not isinstance(saved, dict) or saved.get("kind") is None:
            return None

        key = saved.get("key")

        # JSON has no tuples: an album's key comes back as a list.
        if isinstance(key, list):
            key = tuple(key)

        return {
            "kind": saved["kind"],
            "key": key,
            "ids": frozenset(saved.get("ids") or []),
        }

    def _save_session(self):
        """Keep the page shown, the queue and the position in the track being
        played, for the next start (see ``_restore_session``)."""
        manager = self.playback_manager
        loaded = manager.current_track is not None
        source = manager.source
        saved_source = None

        if source is not None:
            key = source["key"]
            saved_source = {
                "kind": source["kind"],
                "key": list(key) if isinstance(key, tuple) else key,
                "ids": list(source["ids"]),
            }

        save_session({
            "page": self._current_page,
            "queue": [track.get("id") for track in manager.queue],
            "index": manager.current_index if loaded else -1,
            "position": manager.get_resume_position() if loaded else 0.0,
            "source": saved_source,
        })

    def _on_close_request(self, window):
        # What is on screen and playing is picked up on the next start. Not
        # saved before the library has been restored: the session on disk is
        # then still the one to keep.
        if self._session_restored:
            self._save_session()

        # A slider moved a moment ago may not have been saved yet.
        self.equalizer.flush()
        self.library_watcher.stop()
        self.mpris.close()
        self.playback_manager.close()
        self._discard_downloads()
        return False