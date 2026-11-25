"""Dropping files onto a page.

``enable_file_drop`` turns a widget into a place where files can be dropped
from the file manager. While files are dragged over it, it is outlined, so
the person can see where they can go.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

_CSS = """
.file-drop-active {
    background-color: alpha(@accent_bg_color, 0.1);
    outline: 2px dashed @accent_bg_color;
    outline-offset: -6px;
    border-radius: 12px;
}
"""
_css_installed = False


def _install_css():
    global _css_installed

    if _css_installed:
        return

    display = Gdk.Display.get_default()

    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_installed = True


def enable_file_drop(widget, on_files, accept=None):
    """Let files be dropped on ``widget``.

    ``on_files(paths)`` is called with a list of ``Path`` (the files that
    have a place on this computer, in the order they were dropped). It runs
    a moment after the drop, once the drag is over, so it can open windows.

    ``accept()``, if given, says whether a drop is welcome right now: while it
    returns False the widget does not react to files dragged over it.
    """
    _install_css()

    # Copying is all that is offered: dropped files are copied into the
    # library, never moved out of where they are.
    target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

    if accept is not None:
        target.connect("accept", lambda _target, _drop: bool(accept()))

    def on_current_drop_changed(drop_target, _pspec):
        if drop_target.get_current_drop() is not None:
            widget.add_css_class("file-drop-active")
        else:
            widget.remove_css_class("file-drop-active")

    def on_drop(_target, value, _x, _y):
        paths = []

        for file in value.get_files():
            path = file.get_path()

            if path:
                paths.append(Path(path))

        if not paths:
            return False

        # After the drag is over: the file manager that sent the files waits
        # for the answer, and the window that opens next would hold it up.
        def deliver():
            on_files(paths)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)
        return True

    target.connect("notify::current-drop", on_current_drop_changed)
    target.connect("drop", on_drop)
    widget.add_controller(target)
    return target
