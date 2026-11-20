import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.collection_actions import (
    SORT_OPTIONS,
    attach_track_context_menu,
    open_artist_named,
    sort_tracks,
    toggle_track_playback,
    track_is_playing,
)
from melody.layout import style_header_controls
from melody.services.track_range import track_length
from melody.services.compilations import is_compilation
from melody.services.settings import get_settings
from melody.services.singles import has_album
from melody.widgets.collection_grid import CollectionGrid
from melody.widgets.cover_edit_button import with_edit_button
from melody.widgets.import_dialogs import create_import_button
from melody.widgets.value_label import value_label


class AlbumsView(Gtk.Box):
    ALBUM_COVER_SIZE = 64

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.tracks = []
        self.albums = []
        self.current_album = None
        self.playback_manager = None
        self._edit_track = None
        self._choose_playlist = None
        self.on_rename_album = None
        self.on_rename_album_artist = None
        self.on_change_album_image = None
        self.on_import = None

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

    def set_playback_manager(self, playback_manager):
        self.playback_manager = playback_manager
        GLib.timeout_add(250, self._update_play_buttons)

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist

    def set_rename_callbacks(self, title_callback, artist_callback):
        self.on_rename_album = title_callback
        self.on_rename_album_artist = artist_callback

    def set_image_callbacks(self, album_callback):
        self.on_change_album_image = album_callback

    def set_import_callback(self, callback):
        """``callback(context)`` imports music from an album's page; the
        context names the album and its artist, the starting values of what
        is imported."""
        self.on_import = callback

    # ------------------------------------------------------------------
    # Navigation from a track's menu
    # ------------------------------------------------------------------

    @staticmethod
    def _album_key_of_track(track):
        """(album artist, album title), case-insensitive: what identifies an
        album, however the track got there."""
        return (
            str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            ).casefold(),
            str(track.get("album") or "").casefold(),
        )

    def _album_of_track(self, track):
        """The album ``track`` belongs to.

        It is found by name, not by looking for the track in the albums: a
        compilation is left out of the albums unless the setting says
        otherwise, but it still belongs to its album, which must stay
        reachable from the compilation's menu. When the album is not listed at
        all (it only holds compilations), it is built from every track that
        shares its name.
        """
        key = self._album_key_of_track(track)

        # A single has no album to find.
        if not has_album(track):
            return None

        for album in self.albums:
            if (album["artist"].casefold(), album["title"].casefold()) == key:
                return album

        tracks = [
            candidate for candidate in self.tracks
            if self._album_key_of_track(candidate) == key
        ]

        if not tracks:
            return None

        tracks.sort(key=lambda item: str(item.get("title") or "").casefold())
        first = tracks[0]

        return {
            "artist": str(
                first.get("album_artist")
                or first.get("artist")
                or "Unknown Artist"
            ),
            "title": str(first.get("album") or "Unknown Album"),
            "tracks": tracks,
            "artwork_path": next(
                (
                    item["album_artwork_path"]
                    for item in tracks
                    if item.get("album_artwork_path")
                ),
                None,
            ),
        }

    def open_album_of_track(self, track):
        """Show the details of the album ``track`` is in. False if unknown."""
        album = self._album_of_track(track)

        if album is None:
            return False

        self._show_album(album)
        return True

    def is_album_open(self, track):
        """True when the details page of ``track``'s album is showing."""
        current = self.current_album

        return (
            current is not None
            and self.stack.get_visible_child_name() == "details"
            and (current["artist"].casefold(), current["title"].casefold())
            == self._album_key_of_track(track)
        )

    # ------------------------------------------------------------------
    # Header and grid
    # ------------------------------------------------------------------

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        title = Gtk.Label(label="Albums", xalign=0)
        title.add_css_class("title-2")
        title.set_hexpand(True)

        self.count_label = Gtk.Label(label="0 albums")
        self.count_label.add_css_class("dim-label")

        self.sort_dropdown = Gtk.DropDown.new_from_strings([
            "Album title",
            "Artist",
        ])
        self.sort_dropdown.set_selected(0)
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search albums…")
        style_header_controls(self.sort_dropdown, self.search_entry)
        self.search_entry.connect("search-changed", self._on_search_changed)

        header.append(title)
        header.append(self.count_label)
        header.append(self.sort_dropdown)
        header.append(self.search_entry)

        # Same 12px as the spacing between the header's own controls, so the
        # grid never starts flush against the bottom of the search bar.
        header.set_margin_bottom(12)
        self.grid_header = header

    def _build_grid(self):
        self.grid = CollectionGrid(
            on_open=self._show_album,
            placeholder_icon="media-optical-symbolic",
        )

        self.empty_page = Adw.StatusPage(
            title="No albums yet",
            description="Your albums will appear here.",
            icon_name="media-optical-symbolic",
        )

        self.grid_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.grid_page.append(self.grid_header)

        # "No matching albums" is shown here, under the header, so the search
        # bar stays on screen and the search can be changed.
        self.no_match_page = Adw.StatusPage(
            title="No matching albums",
            description="Try a different search.",
            icon_name="media-optical-symbolic",
        )

        self.grid_stack = Gtk.Stack()
        self.grid_stack.set_vexpand(True)
        self.grid_stack.add_named(self.grid, "grid")
        self.grid_stack.add_named(self.no_match_page, "no-match")
        self.grid_page.append(self.grid_stack)

    def set_tracks(self, tracks):
        # Whatever is showing before the refresh (an edit window was just
        # saved, the library was rescanned...) is showing again after it: the
        # grid stays the grid, an open album stays open. The album is found
        # again by the tracks it held rather than by its name, because the
        # edit may well have renamed it.
        open_ids = None

        if (
                self.current_album is not None
                and self.stack.get_visible_child_name() == "details"
        ):
            open_ids = {
                track.get("id") for track in self.current_album["tracks"]
            }

        self.tracks = list(tracks)
        self.current_album = None
        self._build_albums()
        self._refresh_grid()

        if open_ids:
            album = self._find_album_by_tracks(open_ids)

            if album is not None:
                self._show_album(album)

    def _find_album_by_tracks(self, track_ids):
        """The album that now holds most of the tracks in ``track_ids``.

        Editing a track can move it out of its album: the album it was shown
        in is still the one holding the others. If the album was renamed,
        merged or moved to another artist, all of its tracks went along, so it
        is found the same way. None if none of them is left in an album.
        """
        best, best_count = None, 0

        for album in self.albums:
            count = sum(
                1 for track in album["tracks"]
                if track.get("id") in track_ids
            )

            if count > best_count:
                best, best_count = album, count

        if best is not None:
            return best

        # Not listed: an album that only holds compilations, which the
        # setting keeps out of the albums. It is still reachable, so build it
        # from one of its tracks.
        for track in self.tracks:
            if track.get("id") in track_ids:
                album = self._album_of_track(track)

                if album is not None:
                    return album

        return None

    def _build_albums(self):
        albums = {}
        show_compilations = get_settings().show_compilations_in_albums

        for track in self.tracks:
            # A track without an album (a single) is not part of any album.
            if not has_album(track):
                continue

            # A compilation is still part of its album, but only listed in it
            # (and played with it) when the setting says so. An album left
            # with nothing in it is not shown at all.
            if is_compilation(track) and not show_compilations:
                continue

            artist = str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            )
            title = str(track.get("album"))
            key = (artist.casefold(), title.casefold())

            if key not in albums:
                albums[key] = {
                    "artist": artist,
                    "title": title,
                    "tracks": [],
                    "artwork_path": track.get("album_artwork_path"),
                }

            albums[key]["tracks"].append(track)

            if not albums[key]["artwork_path"]:
                albums[key]["artwork_path"] = track.get("album_artwork_path")

        self.albums = list(albums.values())

        for album in self.albums:
            album["tracks"].sort(
                key=lambda track: str(track.get("title") or "").casefold()
            )

    def _refresh_grid(self):
        query = self.search_entry.get_text().strip().casefold()
        visible_albums = [
            album for album in self.albums
            if (
                    not query
                    or query in album["title"].casefold()
                    or query in album["artist"].casefold()
            )
        ]

        sort_by_artist = self.sort_dropdown.get_selected() == 1
        if sort_by_artist:
            visible_albums.sort(
                key=lambda album: (
                    album["artist"].casefold(),
                    album["title"].casefold(),
                )
            )
        else:
            visible_albums.sort(
                key=lambda album: (
                    album["title"].casefold(),
                    album["artist"].casefold(),
                )
            )

        self.grid.populate(
            visible_albums,
            describe=self._describe_album,
            on_play=self._toggle_album,
            play_tooltip="Play album",
            on_edit=self._on_edit_album_from_grid,
            open_in_menu=True,
            on_open_artist=self._on_open_album_artist,
        )

        total = len(self.albums)
        visible = len(visible_albums)

        if total == 0:
            self.empty_page.set_title("No albums yet")
            self.empty_page.set_description("Your albums will appear here.")
            self.stack.set_visible_child_name("empty")
        elif visible == 0:
            # Stay on the grid page: its header holds the search bar.
            self.grid_stack.set_visible_child_name("no-match")
            self.stack.set_visible_child_name("grid")
        else:
            self.grid_stack.set_visible_child_name("grid")
            self.stack.set_visible_child_name("grid")

        self.count_label.set_text(
            f"{visible} of {total} albums"
            if visible != total
            else f"{total} album{'s' if total != 1 else ''}"
        )

        self._update_play_buttons()

    @staticmethod
    def _on_open_album_artist(album):
        open_artist_named(album["artist"])

    def _on_edit_album_from_grid(self, album):
        if self.on_rename_album is not None:
            self.on_rename_album(album)

    @staticmethod
    def _describe_album(album):
        return album["title"], album["artist"], album.get("artwork_path")

    # ------------------------------------------------------------------
    # Album details
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
        back_button.set_tooltip_text("Back to albums")
        back_button.connect("clicked", self._on_back_clicked)

        self.details_heading = Gtk.Label(xalign=0)
        self.details_heading.add_css_class("title-2")
        self.details_heading.set_hexpand(True)

        self.details_sort_dropdown = Gtk.DropDown.new_from_strings(SORT_OPTIONS)
        self.details_sort_dropdown.set_selected(0)
        self.details_sort_dropdown.connect(
            "notify::selected",
            self._on_details_sort_changed,
        )

        header.append(back_button)
        header.append(self.details_heading)
        self.details_page.append(header)

        info = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
        )
        info.set_margin_start(24)
        info.set_margin_end(24)

        self.details_artwork = Gtk.Picture()
        self.details_artwork.set_size_request(200, 200)
        self.details_artwork.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.details_artwork.set_can_shrink(True)

        self.details_placeholder = Gtk.Image.new_from_icon_name(
            "media-optical-symbolic"
        )
        self.details_placeholder.set_pixel_size(96)

        self.details_artwork_stack = Gtk.Stack()
        self.details_artwork_stack.set_size_request(200, 200)
        self.details_artwork_stack.set_valign(Gtk.Align.START)
        self.details_artwork_stack.add_named(self.details_artwork, "artwork")
        self.details_artwork_stack.add_named(self.details_placeholder, "placeholder")

        info_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        info_labels.set_valign(Gtk.Align.START)
        info_labels.set_hexpand(True)

        self.details_album_title = value_label(css_classes=("title-1",))

        self.details_artist = value_label(
            css_classes=("title-3", "dim-label"),
        )

        self.details_track_count = Gtk.Label(xalign=0)
        self.details_track_count.add_css_class("dim-label")

        self.details_play_button = Gtk.Button(label="Play")
        self.details_play_button.set_halign(Gtk.Align.START)
        self.details_play_button.connect("clicked", self._on_details_play_clicked)

        edit_button = Gtk.Button(label="Edit")
        edit_button.connect(
            "clicked",
            lambda _button: self._on_edit_album_clicked(),
        )

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.START)
        actions.append(self.details_play_button)
        actions.append(edit_button)
        actions.append(create_import_button(self._on_import_clicked))

        info_labels.append(self.details_album_title)
        info_labels.append(self.details_artist)
        info_labels.append(self.details_track_count)
        info_labels.append(actions)

        # Sort selector: bottom-right of the header, right above the list.
        self.details_sort_dropdown.set_valign(Gtk.Align.END)
        self.details_sort_dropdown.set_halign(Gtk.Align.END)

        info.append(
            with_edit_button(
                self.details_artwork_stack,
                self._on_change_album_image_clicked,
            )
        )
        info.append(info_labels)
        info.append(self.details_sort_dropdown)
        self.details_page.append(info)

        self.details_page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.track_list = Gtk.ListBox()
        self.track_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.track_list.set_activate_on_single_click(False)
        self.track_list.add_css_class("boxed-list")
        self.track_list.connect("row-activated", self._on_track_activated)

        track_scroll = Gtk.ScrolledWindow()
        track_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        track_scroll.set_vexpand(True)
        track_scroll.set_margin_start(24)
        track_scroll.set_margin_end(24)
        track_scroll.set_margin_bottom(16)
        track_scroll.set_child(self.track_list)

        self.details_page.append(track_scroll)

    def _show_album(self, album):
        self.current_album = album
        self.details_heading.set_text("Album")
        self.details_album_title.set_text(album["title"])
        self.details_artist.set_text(album["artist"])

        track_count = len(album["tracks"])
        self.details_track_count.set_text(
            f"{track_count} track{'s' if track_count != 1 else ''}"
        )

        artwork_path = album.get("artwork_path")
        if artwork_path:
            self.details_artwork.set_filename(artwork_path)
            self.details_artwork_stack.set_visible_child_name("artwork")
        else:
            self.details_artwork.set_filename(None)
            self.details_artwork_stack.set_visible_child_name("placeholder")

        self._refresh_detail_tracks()
        self.stack.set_visible_child_name("details")
        self._update_play_buttons()

    def _create_track_row(self, track):
        row = Gtk.ListBoxRow()
        row.track = track

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)

        title = Gtk.Label(
            label=str(track.get("title") or "Unknown Title"),
            xalign=0,
        )
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_hexpand(True)

        duration = Gtk.Label(
            label=self._format_duration(track_length(track)),
            xalign=1,
        )
        duration.add_css_class("dim-label")

        content.append(title)
        content.append(duration)
        row.set_child(content)

        if self._edit_track is not None:
            attach_track_context_menu(
                row,
                track,
                on_play_pause=self._toggle_track,
                is_playing=lambda t: track_is_playing(self.playback_manager, t),
                on_edit=self._edit_track,
                on_add_to_playlist=self._choose_playlist,
            )

        return row

    def _toggle_track(self, track):
        queue = self.current_album["tracks"] if self.current_album else [track]
        toggle_track_playback(self.playback_manager, track, queue)
        self._update_play_buttons()

    def _refresh_detail_tracks(self):
        child = self.track_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.track_list.remove(child)
            child = next_child

        if self.current_album is None:
            return

        for track in sort_tracks(
                self.current_album["tracks"],
                self.details_sort_dropdown.get_selected(),
        ):
            self.track_list.append(self._create_track_row(track))

    def _on_details_sort_changed(self, dropdown, pspec):
        self._refresh_detail_tracks()

    def _on_edit_album_clicked(self):
        if self.current_album is not None and self.on_rename_album is not None:
            self.on_rename_album(self.current_album)

    def _on_import_clicked(self):
        if self.current_album is not None and self.on_import is not None:
            self.on_import({
                "artist": self.current_album["artist"],
                "album": self.current_album["title"],
            })

    def _on_change_album_image_clicked(self):
        if (
                self.current_album is not None
                and self.on_change_album_image is not None
        ):
            self.on_change_album_image(self.current_album)

    def _on_edit_album_artist_clicked(self):
        if (
                self.current_album is not None
                and self.on_rename_album_artist is not None
        ):
            self.on_rename_album_artist(self.current_album)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _is_album_playing(self, album):
        if self.playback_manager is None:
            return False

        current_track = self.playback_manager.current_track
        if current_track is None:
            return False

        current_path = current_track.get("path")
        return any(track.get("path") == current_path for track in album["tracks"])

    def _update_play_buttons(self):
        if self.playback_manager is None:
            return GLib.SOURCE_CONTINUE

        is_playing_state = self.playback_manager.state == "playing"
        self.grid.update_playing(
            lambda album: is_playing_state and self._is_album_playing(album)
        )

        if self.current_album is not None:
            playing = (
                    self._is_album_playing(self.current_album)
                    and self.playback_manager.state == "playing"
            )
            self.details_play_button.set_label("Pause" if playing else "Play")

        return GLib.SOURCE_CONTINUE

    def _toggle_album(self, album):
        if self.playback_manager is None:
            return

        if self._is_album_playing(album):
            if self.playback_manager.state == "playing":
                self.playback_manager.pause()
            else:
                self.playback_manager.play()
        else:
            self._play_album(album)

        self._update_play_buttons()

    def _play_album(self, album, start_track=None):
        if self.playback_manager is None:
            return

        tracks = album["tracks"]
        if not tracks:
            return

        index = 0
        if start_track is not None:
            for track_index, track in enumerate(tracks):
                if track.get("path") == start_track.get("path"):
                    index = track_index
                    break

        self.playback_manager.play_queue(tracks, index)
        self._update_play_buttons()

    def _on_details_play_clicked(self, button):
        if self.current_album is not None:
            self._toggle_album(self.current_album)

    def _on_track_activated(self, list_box, row):
        if self.current_album is not None:
            self._play_album(self.current_album, row.track)

    # ------------------------------------------------------------------
    # Navigation, search and sorting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration(duration):
        duration = max(0, int(duration or 0))
        minutes, seconds = divmod(duration, 60)
        return f"{minutes}:{seconds:02}"

    def _on_search_changed(self, search_entry):
        self._refresh_grid()

    def _on_sort_changed(self, dropdown, pspec):
        self._refresh_grid()

    def _on_back_clicked(self, button):
        # No album is open any more: a refresh must not bring it back.
        self.current_album = None
        self.stack.set_visible_child_name("grid")