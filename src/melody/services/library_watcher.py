"""Watching the music folder, to scan it again when files change.

The music folder is Melody's, but it is also an ordinary folder: music copied
into it by hand, files deleted or renamed in the file manager... ``LibraryWatcher``
notices, and calls ``on_change()`` once things have calmed down, so the library
can follow without the person having to press "Rescan".

How it works:

* Gio can only watch one folder at a time, not a folder and everything under
  it. So there is one watch for each folder of the tree (the artists, their
  albums...), and a folder that appears gets watched too.
* ``on_change()`` is called once there have been no changes for
  ``QUIET_SECONDS``: copying an album makes hundreds of changes, which must
  make one scan, run when the copy is over.
* Some changes are Melody's own: a scan moves files into their artist and album
  folders, an edit renames files, an import copies them. They must not start
  yet another scan, which would move files, which would... So the window
  tells the watcher when it is busy with the folder (``pause`` / ``resume``
  around a scan, ``ignore_for`` after its own edits), and changes made then
  are not reported.
* Hidden files and folders, and the temporary files of downloads and editors
  (``.part``, ``.tmp``, ``~``), are not changes worth a scan.

Nothing here reads or writes the music itself.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib

# How long the folder must stay quiet before the change is reported, in
# seconds. Long enough for a copy that pauses now and then.
QUIET_SECONDS = 3.0

# After a scan, and after Melody's own edits, the changes that were made are
# still being announced for a moment (Gio groups them): they are Melody's, not
# something to scan for.
GRACE_SECONDS = 2.0

# Changes that say nothing about the files themselves.
_IGNORED_EVENTS = {
    Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
    Gio.FileMonitorEvent.PRE_UNMOUNT,
    Gio.FileMonitorEvent.UNMOUNTED,
}

# Files that are on their way to being something else.
_TEMPORARY_SUFFIXES = (".part", ".partial", ".crdownload", ".tmp", ".temp", "~")


class LibraryWatcher:
    """Calls ``on_change()`` when files in ``folder`` were added, changed or
    removed by something else than Melody."""

    def __init__(self, folder, on_change, quiet_seconds=QUIET_SECONDS):
        self.folder = Path(folder)
        self._on_change = on_change
        self._quiet_seconds = quiet_seconds

        # One Gio monitor for each folder watched.
        self._monitors: dict[Path, Gio.FileMonitor] = {}
        self._started = False
        self._paused = False
        # Changes before this moment (``time.monotonic()``) are not reported.
        self._ignore_until = 0.0
        # The source waiting for the quiet (0 for none).
        self._timer = 0

    # ------------------------------------------------------------------
    # Starting and stopping
    # ------------------------------------------------------------------

    def start(self):
        """Begin watching (nothing happens if it already is)."""
        self._started = True
        self._sync_monitors()

    def stop(self):
        """Stop watching, and forget any change still waiting to be
        reported."""
        self._started = False
        self._cancel_timer()

        for monitor in self._monitors.values():
            monitor.cancel()

        self._monitors.clear()

    def pause(self):
        """Melody is working on the folder (a scan): report nothing, and drop
        what was waiting, since the scan sees it all anyway."""
        self._paused = True
        self._cancel_timer()

    def resume(self, grace=GRACE_SECONDS):
        """Melody is done. Changes made in the last moments are still
        Melody's, so reporting starts again after ``grace`` seconds."""
        self._paused = False
        self.ignore_for(grace)

        # Folders were made and removed meanwhile.
        if self._started:
            self._sync_monitors()

    def ignore_for(self, seconds):
        """Do not report the changes made now and in the next ``seconds``
        seconds (Melody just changed files itself)."""
        self._ignore_until = max(
            self._ignore_until,
            time.monotonic() + seconds,
        )
        self._cancel_timer()

    # ------------------------------------------------------------------
    # What is watched
    # ------------------------------------------------------------------

    def _sync_monitors(self):
        """Watch exactly the folders that exist now: the new ones are added,
        the ones that are gone are let go."""
        wanted = set()

        if self.folder.is_dir():
            for directory, subfolders, _files in os.walk(self.folder):
                # Hidden folders (the trash, caches...) are none of our
                # business.
                subfolders[:] = [
                    name for name in subfolders if not name.startswith(".")
                ]
                wanted.add(Path(directory))

        for path in list(self._monitors):
            if path not in wanted:
                self._monitors.pop(path).cancel()

        for path in sorted(wanted):
            self._watch(path)

    def _watch_tree(self, root):
        """Watch ``root`` and the folders under it (an album that was just
        copied in comes with its own folders)."""
        for directory, subfolders, _files in os.walk(root):
            subfolders[:] = [
                name for name in subfolders if not name.startswith(".")
            ]
            self._watch(Path(directory))

    def _watch(self, path):
        if path in self._monitors:
            return

        try:
            monitor = Gio.File.new_for_path(str(path)).monitor_directory(
                Gio.FileMonitorFlags.NONE,
                None,
            )
        except GLib.Error as error:
            # Most likely the system's limit of watched folders is reached:
            # what is already watched keeps working.
            print(f"Could not watch {path}: {error.message}")
            return

        monitor.connect("changed", self._on_event)
        self._monitors[path] = monitor

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    def _is_noise(self, path):
        try:
            parts = path.relative_to(self.folder).parts
        except ValueError:
            parts = path.parts

        return (
            any(part.startswith(".") for part in parts)
            or path.name.endswith(_TEMPORARY_SUFFIXES)
        )

    def _on_event(self, _monitor, file, _other_file, event):
        if event in _IGNORED_EVENTS:
            return

        location = file.get_path()

        if not location:
            return

        path = Path(location)

        if self._is_noise(path):
            return

        # A folder that appeared (an album copied in) is watched from now on,
        # whoever made it. Its files were maybe written before the watch
        # began, which is why a new folder is reported like any change.
        if (
                event != Gio.FileMonitorEvent.DELETED
                and path not in self._monitors
                and path.is_dir()
        ):
            self._watch_tree(path)

        if self._paused or time.monotonic() < self._ignore_until:
            return

        self._schedule()

    def _schedule(self):
        """Report in ``QUIET_SECONDS``, unless there is another change by
        then, which starts the wait again."""
        self._cancel_timer()
        self._timer = GLib.timeout_add(
            int(self._quiet_seconds * 1000),
            self._report,
        )

    def _cancel_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _report(self):
        self._timer = 0

        # Melody got busy during the wait: it saw the changes itself.
        if self._paused or time.monotonic() < self._ignore_until:
            return GLib.SOURCE_REMOVE

        self._sync_monitors()
        self._on_change()
        return GLib.SOURCE_REMOVE
