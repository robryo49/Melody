"""A small window shown while something is measured before an import.

With ReplayGain on, the loudness of what is about to be imported is measured
first, so the edit window can start from a ReplayGain that evens it out. That
takes a few seconds for a track: this window says so, and lets the person
skip it.
"""

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk


def show_measuring_dialog(parent, total, run, on_done):
    """Show the window while ``run(progress, should_stop)`` works in a thread
    of its own.

    ``run`` calls ``progress(done, total)`` as it goes, and should return as
    soon as ``should_stop()`` is True (the person clicked Skip). Its result
    is given to ``on_done(result)``, called once the window is closed (also
    after a Skip, with whatever was measured by then).
    """
    state = {"stop": False, "finished": False}

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title("Measuring loudness")
    window.set_resizable(False)
    window.set_default_size(380, -1)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(24)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)

    spinner = Gtk.Spinner()
    spinner.set_size_request(32, 32)
    spinner.start()

    heading = Gtk.Label(
        label=(
            "Measuring the loudness of the track…"
            if total == 1
            else f"Measuring the loudness of {total} tracks…"
        ),
        wrap=True,
    )
    heading.add_css_class("heading")

    note = Gtk.Label(
        label="Its ReplayGain starts out set from it. You can change it in "
              "the next window.",
        wrap=True,
    )
    note.add_css_class("dim-label")
    note.add_css_class("caption")

    bar = Gtk.ProgressBar()
    bar.set_visible(total > 1)

    skip_button = Gtk.Button(label="Skip")
    skip_button.set_halign(Gtk.Align.CENTER)

    content.append(spinner)
    content.append(heading)
    content.append(bar)
    content.append(note)
    content.append(skip_button)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    window.set_content(toolbar)

    def stop(*_args):
        state["stop"] = True
        skip_button.set_sensitive(False)
        skip_button.set_label("Skipping…")

    def on_progress(done, count):
        # Called from the measuring thread.
        def show():
            if not state["finished"]:
                bar.set_fraction(done / count if count else 1.0)

            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)

    def finished(result):
        state["finished"] = True
        window.close()
        on_done(result)
        return GLib.SOURCE_REMOVE

    def work():
        try:
            result = run(on_progress, lambda: state["stop"])
        except Exception:
            result = {}

        GLib.idle_add(finished, result)

    def on_close_request(_window):
        # Closing the window is a Skip; it goes once the work has stopped.
        if state["finished"]:
            return False

        stop()
        return True

    skip_button.connect("clicked", stop)
    window.connect("close-request", on_close_request)

    window.present()
    threading.Thread(target=work, daemon=True).start()
