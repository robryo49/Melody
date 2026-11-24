import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from pathlib import Path

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.collection_actions import (
    album_key,
    album_source,
    attach_track_context_menu,
    is_playback_source,
    open_artist_named,
    playback_source,
    reveal_track_row,
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
from melody.widgets.cover_style import install_cover_css
from melody.widgets.import_dialogs import create_import_button
from melody.widgets.link_label import make_link
from melody.widgets.track_table import (
    ALBUM_COLUMNS,
    TrackSort,
    TrackTableHeader,
    create_track_row,
    enable_multiple_selection,
    link_selections,
    update_favorite_in_list,
    update_play_count_in_list,
)
from melody.widgets.value_label import value_label


class ArtistsView(Gtk.Box):
    ALBUM_COVER_SIZE = 64
    # Same size as the thumbnails in the Library and Compilations lists.
    COMPILATION_THUMBNAIL_SIZE = 32

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.tracks = []
        self.artists = []
        self.current_artist = None
        self.current_album = None
        self.playback_manager = None
        self._edit_track = None
        self._choose_playlist = None
        self.on_rename_artist = None
        self.on_rename_album = None
        self.on_rename_album_artist = None
        self.on_change_artist_image = None
        self.on_change_album_image = None
        self.on_import = None
        # The column each track list is sorted by (clicked in its header).
        self.album_sort = TrackSort()
        self.compilation_sort = TrackSort()
        self.single_sort = TrackSort()

        self._build_header()
        self._build_grid()
        self._build_artist_details()
        self._build_album_details()

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.grid_page, "grid")
        self.stack.add_named(self.artist_page, "artist")
        self.stack.add_named(self.album_page, "album")
        self.stack.add_named(self.empty_page, "empty")

        self.append(self.stack)
        self.stack.set_visible_child_name("empty")

    def set_playback_manager(self, playback_manager):
        self.playback_manager = playback_manager
        GLib.timeout_add(250, self._update_play_buttons)

    def set_context_actions(self, edit_track, choose_playlist):
        self._edit_track = edit_track
        self._choose_playlist = choose_playlist

    def set_rename_callbacks(
            self,
            artist_callback,
            album_title_callback,
            album_artist_callback,
    ):
        self.on_rename_artist = artist_callback
        self.on_rename_album = album_title_callback
        self.on_rename_album_artist = album_artist_callback

    def set_image_callbacks(self, artist_callback, album_callback):
        self.on_change_artist_image = artist_callback
        self.on_change_album_image = album_callback

    def set_import_callback(self, callback):
        """``callback(context)`` imports music from an artist's page or from
        one of their albums' pages; the context names the artist (and the
        album, on an album's page), the starting values of what is
        imported."""
        self.on_import = callback

    # ------------------------------------------------------------------
    # Navigation from a track's menu
    # ------------------------------------------------------------------

    @staticmethod
    def _album_key_of_track(track):
        """(album artist, album title), case-insensitive: what identifies an
        album, even for a compilation that is not listed in it."""
        return (
            str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            ).casefold(),
            str(track.get("album") or "").casefold(),
        )

    def _artist_of_track(self, track):
        """The artist page to open for ``track``.

        Pages are per album artist. The track's own artist wins when it has a
        page (so a compilation track opens its performer); otherwise it is the
        artist of the album the track is in.
        """
        name = str(track.get("artist") or "").casefold()

        for artist in self.artists:
            if artist["name"].casefold() == name:
                return artist

        track_id = track.get("id")

        for artist in self.artists:
            if any(
                candidate.get("id") == track_id
                for candidate in (*artist["compilations"], *artist["singles"])
            ):
                return artist

            for album in artist["albums"]:
                if any(candidate.get("id") == track_id for candidate in album["tracks"]):
                    return artist

        return None

    def open_artist_of_track(self, track):
        """Show the page of ``track``'s artist. False if unknown."""
        artist = self._artist_of_track(track)

        if artist is None:
            return False

        self._show_artist(artist)
        return True

    def is_album_open(self, track):
        """True when the page of ``track``'s album is showing."""
        current = self.current_album

        return (
            current is not None
            and self.stack.get_visible_child_name() == "album"
            and (current["artist"].casefold(), current["title"].casefold())
            == self._album_key_of_track(track)
        )

    def is_artist_open(self, track):
        """True when the page of ``track``'s artist is showing."""
        artist = self._artist_of_track(track)
        current = self.current_artist

        return (
            artist is not None
            and current is not None
            and self.stack.get_visible_child_name() == "artist"
            and current["name"].casefold() == artist["name"].casefold()
        )

    def view_artist(self, track_ids, track):
        """Show the page of the artist holding ``track_ids`` (what a queue was
        started from) and scroll to ``track`` if it is listed there, which a
        single or a compilation is. False if that artist is not found."""
        artist = self._find_artist_by_tracks(set(track_ids))

        if artist is None:
            return False

        self._show_artist(artist)

        for list_box in (self.artist_single_list, self.artist_compilation_list):
            if reveal_track_row(list_box, track) is not None:
                break

        return True

    def _build_header(self):
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        title = Gtk.Label(label="Artists", xalign=0)
        title.add_css_class("title-2")
        title.set_hexpand(True)

        self.count_label = Gtk.Label(label="0 artists")
        self.count_label.add_css_class("dim-label")

        self.sort_dropdown = Gtk.DropDown.new_from_strings([
            "Artist name",
            "Album count",
            "Track count",
        ])
        self.sort_dropdown.set_selected(0)
        self.sort_dropdown.connect(
            "notify::selected",
            self._on_sort_changed,
        )

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search artists…")
        style_header_controls(self.sort_dropdown, self.search_entry)
        self.search_entry.connect(
            "search-changed",
            self._on_search_changed,
        )

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
            on_open=self._show_artist,
            placeholder_icon="avatar-default-symbolic",
            round_covers=True,
        )

        self.empty_page = Adw.StatusPage(
            title="No artists yet",
            description="Your artists will appear here.",
            icon_name="system-users-symbolic",
        )

        self.grid_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.grid_page.append(self.grid_header)

        # "No matching artists" is shown here, under the header, so the search
        # bar stays on screen and the search can be changed.
        self.no_match_page = Adw.StatusPage(
            title="No matching artists",
            description="Try a different search.",
            icon_name="system-users-symbolic",
        )

        self.grid_stack = Gtk.Stack()
        self.grid_stack.set_vexpand(True)
        self.grid_stack.add_named(self.grid, "grid")
        self.grid_stack.add_named(self.no_match_page, "no-match")
        self.grid_page.append(self.grid_stack)

    def _build_artist_details(self):
        self.artist_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        back_button = Gtk.Button.new_from_icon_name(
            "go-previous-symbolic"
        )
        back_button.set_tooltip_text("Back to artists")
        back_button.connect("clicked", self._on_artist_back_clicked)

        self.artist_heading = Gtk.Label(xalign=0)
        self.artist_heading.add_css_class("title-2")
        self.artist_heading.set_hexpand(True)

        header.append(back_button)
        header.append(self.artist_heading)
        self.artist_page.append(header)

        self.artist_info = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
        )
        self.artist_info.set_margin_start(24)
        self.artist_info.set_margin_end(24)

        self.artist_artwork = Gtk.Picture()
        self.artist_artwork.set_size_request(200, 200)
        self.artist_artwork.set_content_fit(Gtk.ContentFit.COVER)
        self.artist_artwork.set_can_shrink(True)

        self.artist_placeholder = Gtk.Image.new_from_icon_name(
            "avatar-default-symbolic"
        )
        self.artist_placeholder.set_pixel_size(96)

        self.artist_artwork_stack = Gtk.Stack()
        self.artist_artwork_stack.set_size_request(200, 200)
        self.artist_artwork_stack.set_valign(Gtk.Align.START)
        self.artist_artwork_stack.add_named(
            self.artist_artwork,
            "artwork",
        )
        self.artist_artwork_stack.add_named(
            self.artist_placeholder,
            "placeholder",
        )
        self.artist_artwork_stack.set_visible_child_name("placeholder")

        # An artist's picture is round, and so is the disc behind the default
        # icon when they have none.
        install_cover_css()
        self.artist_artwork_stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.artist_artwork_stack.add_css_class("round-cover")
        self.artist_placeholder.add_css_class("cover-placeholder")

        artist_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        artist_labels.set_valign(Gtk.Align.START)
        artist_labels.set_hexpand(True)

        self.artist_name = value_label(css_classes=("title-1",))

        self.artist_album_count = Gtk.Label(xalign=0)
        self.artist_album_count.add_css_class("dim-label")

        self.artist_track_count = Gtk.Label(xalign=0)
        self.artist_track_count.add_css_class("dim-label")

        self.artist_play_button = Gtk.Button(label="Play")
        self.artist_play_button.set_halign(Gtk.Align.START)
        self.artist_play_button.connect(
            "clicked",
            self._on_artist_play_clicked,
        )

        artist_edit_button = Gtk.Button(label="Edit")
        artist_edit_button.connect(
            "clicked",
            lambda _button: self._on_edit_artist_clicked(),
        )

        artist_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        artist_actions.set_halign(Gtk.Align.START)
        artist_actions.append(self.artist_play_button)
        artist_actions.append(artist_edit_button)
        artist_actions.append(
            create_import_button(self._on_import_to_artist_clicked)
        )

        artist_labels.append(self.artist_name)
        artist_labels.append(self.artist_album_count)
        artist_labels.append(self.artist_track_count)
        artist_labels.append(artist_actions)

        self.artist_info.append(
            with_edit_button(
                self.artist_artwork_stack,
                self._on_change_artist_image_clicked,
                # Further in than usual, so it stays on the round picture.
                margin=20,
            )
        )
        self.artist_info.append(artist_labels)
        self.artist_page.append(self.artist_info)

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        self.artist_page.append(separator)

        self.artist_album_list = Gtk.ListBox()
        self.artist_album_list.set_selection_mode(
            Gtk.SelectionMode.NONE
        )
        self.artist_album_list.add_css_class("boxed-list")

        album_scroll = Gtk.ScrolledWindow()
        album_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        album_scroll.set_vexpand(True)
        album_scroll.set_margin_start(24)
        album_scroll.set_margin_end(24)
        album_scroll.set_margin_bottom(16)
        # Each kind of music gets its own titled section, in this order:
        # albums, compilations, singles. A section is hidden when the artist
        # has nothing in it.
        self.artist_albums_heading = Gtk.Label(
            label="Albums",
            xalign=0,
        )
        self.artist_albums_heading.add_css_class("title-4")

        # The artist's compilations: a list like the albums one above, but
        # made of track rows.
        self.artist_compilations_heading = Gtk.Label(
            label="Compilations",
            xalign=0,
        )
        self.artist_compilations_heading.add_css_class("title-4")

        self.artist_compilation_list = Gtk.ListBox()
        # Several tracks can be selected (Ctrl or Shift + click) to act on
        # them together, from their right-click menu.
        enable_multiple_selection(self.artist_compilation_list)
        self.artist_compilation_list.set_activate_on_single_click(False)
        self.artist_compilation_list.add_css_class("boxed-list")
        self.artist_compilation_list.connect(
            "row-activated",
            self._on_compilation_activated,
        )

        # The artist's singles (tracks without an album): track rows too.
        self.artist_singles_heading = Gtk.Label(
            label="Singles",
            xalign=0,
        )
        self.artist_singles_heading.add_css_class("title-4")

        self.artist_single_list = Gtk.ListBox()
        enable_multiple_selection(self.artist_single_list)
        self.artist_single_list.set_activate_on_single_click(False)
        self.artist_single_list.add_css_class("boxed-list")
        self.artist_single_list.connect(
            "row-activated",
            self._on_single_activated,
        )

        # The compilations and the singles are two lists on one page: only
        # one of them holds the selection a right click acts on.
        link_selections(
            self.artist_compilation_list,
            self.artist_single_list,
        )

        artist_sections = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )
        artist_sections.append(self.artist_albums_heading)
        artist_sections.append(self.artist_album_list)
        artist_sections.append(self.artist_compilations_heading)
        self.artist_compilation_table = self._create_track_table(
            self.compilation_sort,
            self.artist_compilation_list,
        )
        artist_sections.append(self.artist_compilation_table)
        artist_sections.append(self.artist_singles_heading)
        self.artist_single_table = self._create_track_table(
            self.single_sort,
            self.artist_single_list,
        )
        artist_sections.append(self.artist_single_table)

        album_scroll.set_child(artist_sections)
        self.artist_page.append(album_scroll)

    def _create_track_table(self, sort, list_box):
        """A list of tracks under its column names (which sort it). The names
        scroll with the page here, as the list is one of several sections."""
        header = TrackTableHeader(
            sort,
            self._refresh_artist_track_lists,
            columns=ALBUM_COLUMNS,
            with_artwork=True,
        )

        table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        table.append(header)
        table.append(list_box)
        return table

    def _build_album_details(self):
        self.album_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(16)

        back_button = Gtk.Button.new_from_icon_name(
            "go-previous-symbolic"
        )
        back_button.set_tooltip_text("Back to artist")
        back_button.connect("clicked", self._on_album_back_clicked)

        self.album_heading = Gtk.Label(xalign=0)
        self.album_heading.add_css_class("title-2")
        self.album_heading.set_hexpand(True)

        header.append(back_button)
        header.append(self.album_heading)
        self.album_page.append(header)

        info = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
        )
        info.set_margin_start(24)
        info.set_margin_end(24)

        self.album_artwork = Gtk.Picture()
        self.album_artwork.set_size_request(200, 200)
        self.album_artwork.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.album_artwork.set_can_shrink(True)

        self.album_placeholder = Gtk.Image.new_from_icon_name(
            "media-optical-symbolic"
        )
        self.album_placeholder.set_pixel_size(96)

        self.album_artwork_stack = Gtk.Stack()
        self.album_artwork_stack.set_size_request(200, 200)
        self.album_artwork_stack.set_valign(Gtk.Align.START)
        self.album_artwork_stack.add_named(
            self.album_artwork,
            "artwork",
        )
        self.album_artwork_stack.add_named(
            self.album_placeholder,
            "placeholder",
        )
        self.album_artwork_stack.set_visible_child_name("placeholder")

        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        labels.set_valign(Gtk.Align.START)
        labels.set_hexpand(True)

        self.album_title = value_label(css_classes=("title-1",))

        self.album_artist = value_label(
            css_classes=("title-3", "dim-label"),
        )
        # Clicking the artist opens the artist's page.
        make_link(self.album_artist, self._on_album_artist_link_clicked)

        self.album_track_count = Gtk.Label(xalign=0)
        self.album_track_count.add_css_class("dim-label")

        self.album_play_button = Gtk.Button(label="Play")
        self.album_play_button.set_halign(Gtk.Align.START)
        self.album_play_button.connect(
            "clicked",
            self._on_album_play_clicked,
        )

        album_edit_button = Gtk.Button(label="Edit")
        album_edit_button.connect(
            "clicked",
            lambda _button: self._on_edit_artist_album_clicked(),
        )

        album_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        album_actions.set_halign(Gtk.Align.START)
        album_actions.append(self.album_play_button)
        album_actions.append(album_edit_button)
        album_actions.append(
            create_import_button(self._on_import_to_album_clicked)
        )

        labels.append(self.album_title)
        labels.append(self.album_artist)
        labels.append(self.album_track_count)
        labels.append(album_actions)

        info.append(
            with_edit_button(
                self.album_artwork_stack,
                self._on_change_artist_album_image_clicked,
            )
        )
        info.append(labels)
        self.album_page.append(info)

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        self.album_page.append(separator)

        self.album_track_list = Gtk.ListBox()
        enable_multiple_selection(self.album_track_list)
        self.album_track_list.set_activate_on_single_click(False)
        self.album_track_list.add_css_class("boxed-list")
        self.album_track_list.connect(
            "row-activated",
            self._on_album_track_activated,
        )

        track_scroll = Gtk.ScrolledWindow()
        track_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        track_scroll.set_vexpand(True)
        track_scroll.set_margin_start(24)
        track_scroll.set_margin_end(24)
        track_scroll.set_margin_bottom(16)
        track_scroll.set_child(self.album_track_list)

        # The column names stay above the scrolled rows, in view while the
        # list scrolls; they sort the list when clicked.
        album_header = TrackTableHeader(
            self.album_sort,
            self._refresh_album_track_list,
            columns=ALBUM_COLUMNS,
            margin_start=24,
            margin_end=24,
        )

        album_table = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        album_table.set_vexpand(True)
        album_table.append(album_header)
        album_table.append(track_scroll)

        self.album_page.append(album_table)

    @staticmethod
    def _artist_track_ids(artist):
        """The id of every track on ``artist``'s page."""
        return {
            track.get("id")
            for track in (
                *(
                    track
                    for album in artist["albums"]
                    for track in album["tracks"]
                ),
                *artist["compilations"],
                *artist["singles"],
            )
        }

    def _find_album_by_tracks(self, track_ids):
        """``(artist, album)`` now holding most of ``track_ids`` (or None).

        An album that was renamed, merged, or given another album artist took
        all its tracks along, and an album that lost one track to an edit
        still holds the others, so either way it is the one found.
        """
        best, best_count = None, 0

        for artist in self.artists:
            for album in artist["albums"]:
                count = len(
                    track_ids & {track.get("id") for track in album["tracks"]}
                )

                if count > best_count:
                    best, best_count = (artist, album), count

        return best

    def _find_artist_by_tracks(self, track_ids):
        """The artist now holding most of ``track_ids`` (or None)."""
        best, best_count = None, 0

        for artist in self.artists:
            count = len(track_ids & self._artist_track_ids(artist))

            if count > best_count:
                best, best_count = artist, count

        return best

    def set_tracks(self, tracks):
        # Whatever page is showing before the refresh (an edit window was
        # just saved, the library was rescanned...) is showing again after
        # it: the grid stays the grid, an artist's page stays that artist's
        # page, an album's page stays that album's page. Artists and albums
        # are found again by the tracks they held rather than by their name,
        # because the edit may well have renamed them.
        page = self.stack.get_visible_child_name()
        album_ids = None
        artist_ids = None

        if page == "album" and self.current_album is not None:
            album_ids = {
                track.get("id") for track in self.current_album["tracks"]
            }

        if page in ("artist", "album") and self.current_artist is not None:
            artist_ids = self._artist_track_ids(self.current_artist)

        self.tracks = list(tracks)
        self.current_artist = None
        self.current_album = None
        self._build_artists()
        self._refresh_grid()

        if album_ids:
            found = self._find_album_by_tracks(album_ids)

            if found is not None:
                artist, album = found
                self._show_artist(artist)
                self._show_album(album)
                return

        # On an artist's page, or the album is gone: the artist's page is the
        # closest thing left to where the person was.
        if artist_ids:
            artist = self._find_artist_by_tracks(artist_ids)

            if artist is not None:
                self._show_artist(artist)

    def _build_artists(self):
        artists = {}

        for track in self.tracks:
            artist_name = str(
                track.get("album_artist")
                or track.get("artist")
                or "Unknown Artist"
            )
            album_title = str(track.get("album") or "")

            artist_key = artist_name.casefold()
            album_key = album_title.casefold()

            if artist_key not in artists:
                artists[artist_key] = {
                    "name": artist_name,
                    "albums": {},
                    "compilations": [],
                    "singles": [],
                    "artwork_path": None,
                }

            artist = artists[artist_key]

            # Every compilation is listed under its artist. It is also part
            # of its album only when the setting says so (and only if it has
            # an album at all).
            if is_compilation(track):
                artist["compilations"].append(track)

                if (
                        not get_settings().show_compilations_in_albums
                        or not has_album(track)
                ):
                    continue
            elif not has_album(track):
                # No album: a single, listed under its artist on its own.
                artist["singles"].append(track)
                continue

            if album_key not in artist["albums"]:
                artist["albums"][album_key] = {
                    "title": album_title,
                    "artist": artist_name,
                    "tracks": [],
                    "artwork_path": track.get("album_artwork_path"),
                }

            album = artist["albums"][album_key]
            album["tracks"].append(track)

            if not album["artwork_path"]:
                album["artwork_path"] = track.get(
                    "album_artwork_path"
                )

        self.artists = []

        for artist in artists.values():
            albums = list(artist["albums"].values())

            for album in albums:
                album["tracks"].sort(
                    key=lambda track: str(
                        track.get("title") or ""
                    ).casefold()
                )

            albums.sort(key=lambda album: album["title"].casefold())

            artist["albums"] = albums
            artist["compilations"].sort(
                key=lambda track: str(track.get("title") or "").casefold()
            )
            artist["singles"].sort(
                key=lambda track: str(track.get("title") or "").casefold()
            )
            # Album tracks only: compilations and singles are counted on
            # their own.
            artist["track_count"] = sum(
                1
                for album in albums
                for track in album["tracks"]
                if not is_compilation(track)
            )
            all_tracks = [
                track for album in albums for track in album["tracks"]
            ] + artist["compilations"] + artist["singles"]
            # The artist's own picture (an image in their folder), and only
            # that. With none, the artist shows the default icon: never the
            # cover of one of their albums or tracks.
            artist["artwork_path"] = next(
                (
                    track["artist_artwork_path"]
                    for track in all_tracks
                    if track.get("artist_artwork_path")
                ),
                None,
            )

            self.artists.append(artist)

    def _get_filtered_artists(self):
        query = self.search_entry.get_text().strip().casefold()

        if not query:
            return list(self.artists)

        return [
            artist for artist in self.artists
            if query in artist["name"].casefold()
        ]

    def _get_sorted_artists(self, artists):
        sort_index = self.sort_dropdown.get_selected()

        if sort_index == 1:
            return sorted(
                artists,
                key=lambda artist: (
                    -len(artist["albums"]),
                    artist["name"].casefold(),
                ),
            )

        if sort_index == 2:
            return sorted(
                artists,
                key=lambda artist: (
                    -artist["track_count"],
                    artist["name"].casefold(),
                ),
            )

        return sorted(
            artists,
            key=lambda artist: artist["name"].casefold(),
        )

    def _refresh_grid(self):
        visible_artists = self._get_sorted_artists(
            self._get_filtered_artists()
        )

        self.grid.populate(
            visible_artists,
            describe=self._describe_artist,
            on_play=self._toggle_artist,
            play_tooltip="Play artist",
            on_edit=self._on_edit_artist_from_grid,
            open_in_menu=True,
            artwork_kind="artist",
        )

        total = len(self.artists)
        visible = len(visible_artists)

        if total == 0:
            self.empty_page.set_title("No artists yet")
            self.empty_page.set_description(
                "Your artists will appear here."
            )
            self.stack.set_visible_child_name("empty")
        elif visible == 0:
            # Stay on the grid page: its header holds the search bar.
            self.grid_stack.set_visible_child_name("no-match")
            self.stack.set_visible_child_name("grid")
        else:
            self.grid_stack.set_visible_child_name("grid")
            self.stack.set_visible_child_name("grid")

        if visible == total:
            self.count_label.set_text(
                f"{total} artist{'s' if total != 1 else ''}"
            )
        else:
            self.count_label.set_text(f"{visible} of {total} artists")

        self._update_play_buttons()

    def _on_edit_artist_from_grid(self, artist):
        if self.on_rename_artist is not None:
            self.on_rename_artist(artist)

    @staticmethod
    def _describe_artist(artist):
        album_count = len(artist["albums"])
        track_count = artist["track_count"]
        compilation_count = len(artist["compilations"])
        single_count = len(artist["singles"])

        parts = []

        if album_count or not (compilation_count or single_count):
            parts.append(
                f"{album_count} album{'s' if album_count != 1 else ''}"
            )
            parts.append(
                f"{track_count} track{'s' if track_count != 1 else ''}"
            )

        if single_count:
            parts.append(
                f"{single_count} single{'s' if single_count != 1 else ''}"
            )

        if compilation_count:
            parts.append(
                f"{compilation_count} compilation"
                f"{'s' if compilation_count != 1 else ''}"
            )

        return artist["name"], " · ".join(parts), artist.get("artwork_path")

    def _is_artist_playing(self, artist):
        """True when the artist is what is being played (it was started as an
        artist), not merely one of their tracks playing."""
        return is_playback_source(
            self.playback_manager,
            "artist",
            artist["name"].casefold(),
            lambda: self._artist_queue(artist),
        )

    def _is_album_playing(self, album):
        """The same, for an album (shared with the Albums page)."""
        return is_playback_source(
            self.playback_manager,
            "album",
            album_key(album),
            album["tracks"],
        )

    def _update_play_buttons(self):
        if self.playback_manager is None:
            return GLib.SOURCE_CONTINUE

        is_playing_state = self.playback_manager.state == "playing"
        self.grid.update_playing(
            lambda artist: is_playing_state and self._is_artist_playing(artist)
        )

        if self.current_artist is not None:
            playing = (
                    self._is_artist_playing(self.current_artist)
                    and self.playback_manager.state == "playing"
            )
            self.artist_play_button.set_label(
                "Pause" if playing else "Play"
            )

        if self.current_album is not None:
            playing = (
                    self._is_album_playing(self.current_album)
                    and self.playback_manager.state == "playing"
            )
            self.album_play_button.set_label(
                "Pause" if playing else "Play"
            )

        return GLib.SOURCE_CONTINUE

    def _toggle_artist(self, artist):
        if self.playback_manager is None:
            return

        if self._is_artist_playing(artist):
            if self.playback_manager.state == "playing":
                self.playback_manager.pause()
            else:
                self.playback_manager.play()
        else:
            self._play_artist(artist)

        self._update_play_buttons()

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

    def _play_tracks(self, tracks, start_track=None, source=None):
        if self.playback_manager is None or not tracks:
            return

        index = 0

        if start_track is not None:
            for track_index, track in enumerate(tracks):
                if track.get("path") == start_track.get("path"):
                    index = track_index
                    break

        self.playback_manager.play_queue(tracks, index, source)
        self._update_play_buttons()

    def _album_queue(self, album):
        """The tracks of ``album`` in the order to play them: the order its
        page shows them in (as sorted by the selector) while it is open."""
        if (
                album is self.current_album
                and self.stack.get_visible_child_name() == "album"
        ):
            return self.album_sort.sort(album["tracks"])

        return album["tracks"]

    def _play_album(self, album, start_track=None):
        self._play_tracks(
            self._album_queue(album),
            start_track,
            album_source(album),
        )

    @staticmethod
    def _artist_queue(artist, singles=None):
        """What the artist's Play button plays: the albums' tracks, then the
        singles (in the order given, if the page shows them sorted)."""
        return [
            track
            for album in artist["albums"]
            for track in album["tracks"]
        ] + (artist["singles"] if singles is None else singles)

    def _artist_source(self, artist, tracks=None):
        return playback_source(
            "artist",
            artist["name"].casefold(),
            self._artist_queue(artist) if tracks is None else tracks,
        )

    def _play_artist(self, artist):
        tracks = self._artist_queue(artist)
        self._play_tracks(tracks, None, self._artist_source(artist, tracks))

    def play_page(self):
        """Play the album or artist whose page is open, like its Play button
        does. False (and nothing played) on any other page."""
        if self.playback_manager is None:
            return False

        page = self.stack.get_visible_child_name()

        if page == "album" and self.current_album is not None:
            self._play_album(self.current_album)
            return True

        if page == "artist" and self.current_artist is not None:
            if not self._artist_queue(self.current_artist):
                return False

            self._play_artist(self.current_artist)
            return True

        return False

    def _on_artist_play_clicked(self, button):
        if self.current_artist is not None:
            self._toggle_artist(self.current_artist)

    def _on_album_play_clicked(self, button):
        if self.current_album is not None:
            self._toggle_album(self.current_album)

    def _on_compilation_activated(self, list_box, row):
        # Not the artist's Play list (it leaves the compilations out), so
        # the artist does not count as playing.
        if self.current_artist is not None:
            compilations = self.compilation_sort.sort(
                self.current_artist["compilations"]
            )
            self._play_tracks(
                compilations,
                row.track,
                playback_source(
                    "artist-compilations",
                    self.current_artist["name"].casefold(),
                    compilations,
                ),
            )

    def _on_single_activated(self, list_box, row):
        # Like the artist's Play button, but starting from the single that
        # was clicked.
        if self.current_artist is not None:
            tracks = self._artist_queue(
                self.current_artist,
                self.single_sort.sort(self.current_artist["singles"]),
            )
            self._play_tracks(
                tracks,
                row.track,
                self._artist_source(self.current_artist, tracks),
            )

    def _on_album_track_activated(self, list_box, row):
        if self.current_album is not None:
            self._play_album(self.current_album, row.track)

    def _set_artist_artwork(self, artist):
        artwork_path = artist.get("artwork_path")

        if artwork_path:
            self.artist_artwork.set_filename(artwork_path)
            self.artist_artwork_stack.set_visible_child_name("artwork")
        else:
            self.artist_artwork.set_filename(None)
            self.artist_artwork_stack.set_visible_child_name("placeholder")

    def _show_artist(self, artist):
        self.current_artist = artist
        self.current_album = None

        self.artist_heading.set_text("Artist")
        self.artist_name.set_text(artist["name"])

        album_count = len(artist["albums"])
        track_count = artist["track_count"]

        self.artist_album_count.set_text(
            f"{album_count} album{'s' if album_count != 1 else ''}"
        )
        self.artist_track_count.set_text(
            f"{track_count} track{'s' if track_count != 1 else ''}"
            + (
                f" · {len(artist['singles'])} single"
                f"{'s' if len(artist['singles']) != 1 else ''}"
                if artist["singles"] else ""
            )
            + (
                f" · {len(artist['compilations'])} compilation"
                f"{'s' if len(artist['compilations']) != 1 else ''}"
                if artist["compilations"] else ""
            )
        )

        self._set_artist_artwork(artist)

        child = self.artist_album_list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.artist_album_list.remove(child)
            child = next_child

        for album in artist["albums"]:
            self.artist_album_list.append(
                self._create_album_row(album)
            )

        self._refresh_artist_track_lists()

        self.artist_albums_heading.set_visible(bool(artist["albums"]))
        self.artist_album_list.set_visible(bool(artist["albums"]))
        self.artist_compilations_heading.set_visible(
            bool(artist["compilations"])
        )
        self.artist_compilation_table.set_visible(
            bool(artist["compilations"])
        )
        self.artist_singles_heading.set_visible(bool(artist["singles"]))
        self.artist_single_table.set_visible(bool(artist["singles"]))

        self.stack.set_visible_child_name("artist")
        self._update_play_buttons()

    def _refresh_artist_track_lists(self):
        """Build the open artist's compilations and singles lists, in the
        order their column names say."""
        for list_box, key, sort in (
                (self.artist_compilation_list, "compilations", self.compilation_sort),
                (self.artist_single_list, "singles", self.single_sort),
        ):
            child = list_box.get_first_child()

            while child is not None:
                next_child = child.get_next_sibling()
                list_box.remove(child)
                child = next_child

            if self.current_artist is None:
                continue

            for track in sort.sort(self.current_artist[key]):
                list_box.append(
                    self._create_track_row(track, show_artwork=True)
                )

    def _create_album_row(self, album):
        row = Gtk.ListBoxRow()
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("flat")

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        artwork_stack = Gtk.Stack()
        artwork_stack.set_size_request(
            self.ALBUM_COVER_SIZE,
            self.ALBUM_COVER_SIZE,
        )

        artwork = Gtk.Picture()
        artwork.set_size_request(
            self.ALBUM_COVER_SIZE,
            self.ALBUM_COVER_SIZE,
        )
        artwork.set_content_fit(Gtk.ContentFit.COVER)
        artwork.set_can_shrink(True)

        placeholder = Gtk.Image.new_from_icon_name(
            "media-optical-symbolic"
        )
        placeholder.set_pixel_size(32)

        artwork_stack.add_named(artwork, "artwork")
        artwork_stack.add_named(placeholder, "placeholder")

        artwork_path = album.get("artwork_path")

        if artwork_path:
            artwork.set_filename(artwork_path)
            artwork_stack.set_visible_child_name("artwork")
        else:
            artwork_stack.set_visible_child_name("placeholder")

        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        labels.set_valign(Gtk.Align.CENTER)
        labels.set_hexpand(True)

        title = Gtk.Label(label=album["title"], xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("heading")

        track_count = len(album["tracks"])
        subtitle = Gtk.Label(
            label=f"{track_count} track{'s' if track_count != 1 else ''}",
            xalign=0,
        )
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")

        labels.append(title)
        labels.append(subtitle)

        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")

        content.append(artwork_stack)
        content.append(labels)
        content.append(arrow)

        button.set_child(content)
        button.connect(
            "clicked",
            lambda _button, selected_album=album: self._show_album(
                selected_album
            ),
        )

        row.set_child(button)
        return row

    def _show_album(self, album):
        self.current_album = album

        self.album_heading.set_text("Album")
        self.album_title.set_text(album["title"])
        self.album_artist.set_text(album["artist"])

        track_count = len(album["tracks"])
        self.album_track_count.set_text(
            f"{track_count} track{'s' if track_count != 1 else ''}"
        )

        artwork_path = album.get("artwork_path")

        if artwork_path:
            self.album_artwork.set_filename(artwork_path)
            self.album_artwork_stack.set_visible_child_name("artwork")
        else:
            self.album_artwork.set_filename(None)
            self.album_artwork_stack.set_visible_child_name("placeholder")

        child = self.album_track_list.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.album_track_list.remove(child)
            child = next_child

        self._refresh_album_track_list()

        self.stack.set_visible_child_name("album")
        self._update_play_buttons()

    def _create_track_row(self, track, show_artwork=False):
        artwork = None

        if show_artwork:
            # The compilation's artwork, like in the Compilations page.
            artwork = Gtk.Image.new_from_icon_name(
                "audio-x-generic-symbolic"
            )
            # Only the music note is smaller; a real cover (below) is drawn
            # at the full thumbnail size.
            artwork.set_pixel_size(round(self.COMPILATION_THUMBNAIL_SIZE * 0.7))
            artwork.set_size_request(
                self.COMPILATION_THUMBNAIL_SIZE,
                self.COMPILATION_THUMBNAIL_SIZE,
            )

            artwork_path = track.get("artwork_path")

            if artwork_path and Path(artwork_path).is_file():
                artwork.set_from_file(artwork_path)
                artwork.set_pixel_size(self.COMPILATION_THUMBNAIL_SIZE)

        # The artist's page already says whose tracks these are, and a
        # single or compilation has no album of its own to show: only the
        # title, the play count and the duration.
        row = create_track_row(
            track,
            columns=ALBUM_COLUMNS,
            artwork=artwork,
            vertical_margin=6 if show_artwork else 8,
        )

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
        source = None

        if self.current_album:
            queue = self._album_queue(self.current_album)
            source = album_source(self.current_album)
        elif self.current_artist and any(
                candidate.get("id") == track.get("id")
                for candidate in self.current_artist["compilations"]
        ):
            queue = self.compilation_sort.sort(
                self.current_artist["compilations"]
            )
            source = playback_source(
                "artist-compilations",
                self.current_artist["name"].casefold(),
                queue,
            )
        elif self.current_artist and any(
                candidate.get("id") == track.get("id")
                for candidate in self.current_artist["singles"]
        ):
            queue = self._artist_queue(
                self.current_artist,
                self.single_sort.sort(self.current_artist["singles"]),
            )
            source = self._artist_source(self.current_artist, queue)
        else:
            queue = [track]

        toggle_track_playback(self.playback_manager, track, queue, source)
        self._update_play_buttons()

    def _refresh_album_track_list(self):
        child = self.album_track_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.album_track_list.remove(child)
            child = nxt
        if self.current_album is None:
            return
        for track in self.album_sort.sort(self.current_album["tracks"]):
            self.album_track_list.append(self._create_track_row(track))

    def update_play_count(self, track_id, count):
        """A track was played once more: update its row in the lists."""
        for list_box in (
                self.artist_compilation_list,
                self.artist_single_list,
                self.album_track_list,
        ):
            update_play_count_in_list(list_box, track_id, count)

    def update_favorite(self, track_id, favorite):
        """A track was marked or unmarked as a favorite: update its heart."""
        for list_box in (
                self.artist_compilation_list,
                self.artist_single_list,
                self.album_track_list,
        ):
            update_favorite_in_list(list_box, track_id, favorite)

    def _on_edit_artist_clicked(self):
        if self.current_artist is not None and self.on_rename_artist is not None:
            self.on_rename_artist(self.current_artist)

    def _on_import_to_artist_clicked(self):
        # On an artist's page only the artist is a starting value: the album
        # is left to what the file says.
        if self.current_artist is not None and self.on_import is not None:
            self.on_import({"artist": self.current_artist["name"]})

    def _on_import_to_album_clicked(self):
        if self.current_album is not None and self.on_import is not None:
            self.on_import({
                "artist": self.current_album["artist"],
                "album": self.current_album["title"],
            })

    def _on_album_artist_link_clicked(self):
        if self.current_album is not None:
            open_artist_named(self.current_album["artist"])

    def _on_edit_artist_album_clicked(self):
        if self.current_album is not None and self.on_rename_album is not None:
            self.on_rename_album(self.current_album)

    def _on_change_artist_image_clicked(self):
        if (
                self.current_artist is not None
                and self.on_change_artist_image is not None
        ):
            self.on_change_artist_image(self.current_artist)

    def _on_change_artist_album_image_clicked(self):
        if (
                self.current_album is not None
                and self.on_change_album_image is not None
        ):
            self.on_change_album_image(self.current_album)

    def _on_edit_artist_album_artist_clicked(self):
        if (
                self.current_album is not None
                and self.on_rename_album_artist is not None
        ):
            self.on_rename_album_artist(self.current_album)

    @staticmethod
    def _format_duration(duration):
        duration = max(0, int(duration or 0))
        minutes, seconds = divmod(duration, 60)
        return f"{minutes}:{seconds:02}"

    def _on_search_changed(self, search_entry):
        self._refresh_grid()

    def _on_sort_changed(self, dropdown, pspec):
        self._refresh_grid()

    def _on_artist_back_clicked(self, button):
        # Nothing is open any more: a refresh must not bring it back.
        self.current_artist = None
        self.current_album = None
        self.stack.set_visible_child_name("grid")

    def _on_album_back_clicked(self, button):
        # Back on the artist's page, no album is open any more.
        self.current_album = None
        self.stack.set_visible_child_name("artist")