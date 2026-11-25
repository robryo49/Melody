"""The window that analyses tracks' loudness and sets their ReplayGain.

It asks what to analyse (everything, the library, the favorites...). For
albums, artists and playlists it then lists them, with a search bar above the
list, to pick one. After that it shows the progress, which can be cancelled,
and what was done at the end.

The window knows nothing about files or audio. It is given:

* ``scopes``: what can be chosen, in order. A scope is a dictionary with a
  ``"title"`` and either ``"tracks"`` (a function returning the tracks), or
  ``"choices"`` (a function returning a list of
  ``{"title", "subtitle", "tracks"}``, ``"tracks"`` being a list or a function
  returning one) with a ``"search_placeholder"``;
* ``run(tracks, skip_tagged, progress, should_stop)``: does the analysis (it
  is called in a thread of its own) and returns the summary of
  ``replaygain_analysis.analyse_tracks``.
"""

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from melody.services.library_search import normalize

# How many failures the final summary lists.
MAX_FAILURES_SHOWN = 5


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _row(title, subtitle=None):
    """A list row that can be clicked, with the text kept as it is."""
    row = Adw.ActionRow()
    row.set_title(GLib.markup_escape_text(title))

    if subtitle:
        row.set_subtitle(GLib.markup_escape_text(subtitle))

    row.set_activatable(True)
    row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
    return row


class _AnalysisWindow(Adw.Window):
    def __init__(self, parent, scopes, run):
        super().__init__()

        self._scopes = scopes
        self._run = run
        self._stop = False
        self._running = False
        self._closed = False
        self._query = ""

        self.set_title("Analyse ReplayGain")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_destroy_with_parent(True)
        self.set_default_size(460, 580)

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text("Back")
        self.back_button.set_visible(False)
        self.back_button.connect("clicked", self._on_back_clicked)

        header = Adw.HeaderBar()
        header.pack_start(self.back_button)

        self.stack = Gtk.Stack()
        self.stack.add_named(self._build_scope_page(), "scope")
        self.stack.add_named(self._build_choices_page(), "choices")
        self.stack.add_named(self._build_progress_page(), "progress")
        self.stack.add_named(self._build_done_page(), "done")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        return box

    def _build_scope_page(self):
        page = self._page()

        heading = Gtk.Label(label="What should be analysed?", xalign=0)
        heading.add_css_class("title-4")

        note = Gtk.Label(
            label="Each track's loudness is measured and its ReplayGain is "
                  "written into the file. This can take a while for a big "
                  "selection.",
            xalign=0,
        )
        note.set_wrap(True)
        note.set_max_width_chars(1)
        note.add_css_class("dim-label")

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.connect("row-activated", self._on_scope_activated)

        for scope in self._scopes:
            if "choices" in scope:
                row = _row(scope["title"], "Pick from a list")
            else:
                tracks = scope["tracks"]()
                row = _row(scope["title"], _plural(len(tracks), "track"))
                row.tracks = tracks

                if not tracks:
                    row.set_subtitle("No tracks")
                    row.set_sensitive(False)

            row.scope = scope
            listbox.append(row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(listbox)

        # Analysing takes a while: by default the ones already done are not
        # done again.
        self.skip_check = Gtk.CheckButton(
            label="Skip tracks that already have a ReplayGain"
        )
        self.skip_check.set_active(True)

        page.append(heading)
        page.append(note)
        page.append(scrolled)
        page.append(self.skip_check)
        return page

    def _build_choices_page(self):
        page = self._page()

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.choices_list = Gtk.ListBox()
        self.choices_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.choices_list.add_css_class("boxed-list")
        self.choices_list.set_filter_func(self._choice_matches)
        self.choices_list.connect("row-activated", self._on_choice_activated)

        nothing = Gtk.Label(label="No matches")
        nothing.add_css_class("dim-label")
        nothing.set_margin_top(18)
        nothing.set_margin_bottom(18)
        self.choices_list.set_placeholder(nothing)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(self.choices_list)

        page.append(self.search_entry)
        page.append(scrolled)
        return page

    def _build_progress_page(self):
        page = self._page()
        page.set_valign(Gtk.Align.CENTER)
        page.set_vexpand(True)

        self.progress_title = Gtk.Label(label="Analysing…")
        self.progress_title.add_css_class("title-4")

        self.progress_count = Gtk.Label(label="")
        self.progress_count.add_css_class("dim-label")

        self.progress_bar = Gtk.ProgressBar()

        self.progress_track = Gtk.Label(label="")
        self.progress_track.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.progress_track.add_css_class("dim-label")
        self.progress_track.add_css_class("caption")

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_halign(Gtk.Align.CENTER)
        self.cancel_button.connect("clicked", self._on_cancel_clicked)

        page.append(self.progress_title)
        page.append(self.progress_count)
        page.append(self.progress_bar)
        page.append(self.progress_track)
        page.append(self.cancel_button)
        return page

    def _build_done_page(self):
        self.done_page = Adw.StatusPage()
        self.done_page.set_vexpand(True)

        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("pill")
        close_button.set_halign(Gtk.Align.CENTER)
        close_button.connect("clicked", lambda _button: self.close())
        self.done_page.set_child(close_button)
        return self.done_page

    # ------------------------------------------------------------------
    # Choosing
    # ------------------------------------------------------------------

    def _on_scope_activated(self, _listbox, row):
        scope = row.scope

        if "choices" in scope:
            self._show_choices(scope)
        else:
            self._start(row.tracks)

    def _show_choices(self, scope):
        """List what can be picked (the albums, say), with a search bar."""
        while (child := self.choices_list.get_first_child()) is not None:
            self.choices_list.remove(child)

        for choice in scope["choices"]():
            row = _row(choice["title"], choice.get("subtitle"))
            row.choice = choice
            row.search_text = normalize(
                f"{choice['title']} {choice.get('subtitle') or ''}"
            )
            self.choices_list.append(row)

        self._query = ""
        self.search_entry.set_text("")
        self.search_entry.set_placeholder_text(
            scope.get("search_placeholder", "Search…")
        )

        self.back_button.set_visible(True)
        self.stack.set_visible_child_name("choices")
        self.search_entry.grab_focus()

    def _on_search_changed(self, entry):
        self._query = normalize(entry.get_text())
        self.choices_list.invalidate_filter()

    def _choice_matches(self, row):
        return self._query in row.search_text

    def _on_choice_activated(self, _listbox, row):
        tracks = row.choice["tracks"]
        self._start(tracks() if callable(tracks) else tracks)

    def _on_back_clicked(self, _button):
        self.back_button.set_visible(False)
        self.stack.set_visible_child_name("scope")

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def _start(self, tracks):
        tracks = list(tracks)
        self.back_button.set_visible(False)

        if not tracks:
            self._show_done(
                "emblem-important-symbolic",
                "Nothing to analyse",
                "There are no tracks here.",
            )
            return

        skip_tagged = self.skip_check.get_active()

        self._stop = False
        self._running = True
        self.progress_title.set_text("Analysing…")
        self.progress_count.set_text(f"0 of {len(tracks)}")
        self.progress_bar.set_fraction(0.0)
        self.progress_track.set_text("")
        self.cancel_button.set_sensitive(True)
        self.cancel_button.set_label("Cancel")
        self.stack.set_visible_child_name("progress")

        def work():
            try:
                summary = self._run(
                    tracks,
                    skip_tagged,
                    self._report_progress,
                    lambda: self._stop,
                )
            except Exception as error:
                summary = {"error": str(error)}

            GLib.idle_add(self._on_finished, summary)

        threading.Thread(target=work, daemon=True).start()

    def _report_progress(self, done, total, title):
        """Called from the analysis thread."""
        GLib.idle_add(self._on_progress, done, total, title)

    def _on_progress(self, done, total, title):
        if not self._closed and self._running:
            self.progress_count.set_text(f"{done} of {total}")
            self.progress_bar.set_fraction(done / total if total else 1.0)
            self.progress_track.set_text(title)

        return GLib.SOURCE_REMOVE

    def _on_cancel_clicked(self, _button):
        self._stop = True
        self.cancel_button.set_sensitive(False)
        self.cancel_button.set_label("Stopping…")

    def _on_finished(self, summary):
        self._running = False

        if self._closed:
            return GLib.SOURCE_REMOVE

        if "error" in summary:
            self._show_done(
                "dialog-error-symbolic",
                "The analysis failed",
                GLib.markup_escape_text(summary["error"]),
            )
            return GLib.SOURCE_REMOVE

        lines = [f"Analysed {_plural(summary['analysed'], 'track')}."]

        if summary["skipped"]:
            lines.append(
                f"{_plural(summary['skipped'], 'track')} already had a "
                "ReplayGain, and were left as they were."
            )

        if summary["unsupported"]:
            lines.append(
                f"{_plural(summary['unsupported'], 'file')} cannot hold a "
                "ReplayGain tag (Opus, say), and were skipped."
            )

        failed = summary["failed"]

        if failed:
            lines.append(f"{_plural(len(failed), 'track')} failed:")
            lines.extend(f"• {line}" for line in failed[:MAX_FAILURES_SHOWN])

            if len(failed) > MAX_FAILURES_SHOWN:
                lines.append(f"…and {len(failed) - MAX_FAILURES_SHOWN} more.")

        cancelled = summary["cancelled"]

        self._show_done(
            "media-playback-stop-symbolic" if cancelled
            else "emblem-ok-symbolic",
            "Analysis stopped" if cancelled else "Analysis finished",
            GLib.markup_escape_text("\n".join(lines)),
        )
        return GLib.SOURCE_REMOVE

    def _show_done(self, icon, title, description):
        self.done_page.set_icon_name(icon)
        self.done_page.set_title(title)
        self.done_page.set_description(description)
        self.stack.set_visible_child_name("done")

    def _on_close_request(self, _window):
        # Closing the window stops the analysis too.
        self._closed = True
        self._stop = True
        return False


def show_replaygain_analysis_dialog(parent, scopes, run):
    """Open the analysis window (see the top of this file for what ``scopes``
    and ``run`` are)."""
    _AnalysisWindow(parent, scopes, run).present()
