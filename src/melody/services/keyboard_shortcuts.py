"""Keyboard shortcuts: control the player from the keyboard.

Melody's own keys, which work from anywhere in the main window:

    Space                play or pause (with nothing loaded, like the play
                         button: it plays the selected track)
    Left / Right         seek 5 seconds back / forward
    Up / Down            volume up / down (Melody's own volume, not the
                         system's)
    Ctrl+Left / Right    previous / next track
    M                    mute, or bring the volume back
    S                    shuffle on / off
    R                    repeat: off, all, one

The keyboard's media keys (play / pause, next, previous, stop) work too.
Normally the desktop catches them and sends them to Melody through MPRIS (see
``mpris.py``), whatever window has the focus. They are also handled here, for
a desktop that does not, while Melody's window has the focus. The system's
volume keys are left to the system.

The keys are caught before the widget that has the focus sees them, so they do
the same whether the focus is on a list, a button or nowhere in particular.
The price is that Up, Down, Left, Right and Space no longer move around a list
with the keyboard. They are left alone, so they work as usual, while:

* the focus is in a text field (a search box, say): typing comes first;
* the focus is in a menu or a popup;
* a dialog is open on top of the window.

To change a shortcut, change the ``_bind`` calls in ``KeyboardShortcuts``.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

# How far Left and Right seek, in seconds, and how much Up and Down change
# the volume (0 is silence, 1 is full volume).
SEEK_STEP = 5.0
VOLUME_STEP = 0.05

# Only these modifiers count, so that Caps Lock and Num Lock do not change
# what a key does.
_MODIFIERS = int(
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.SHIFT_MASK
    | Gdk.ModifierType.ALT_MASK
    | Gdk.ModifierType.SUPER_MASK
    | Gdk.ModifierType.META_MASK
)
_NONE = 0
_CTRL = int(Gdk.ModifierType.CONTROL_MASK)


class KeyboardShortcuts:
    """Catches the shortcut keys of ``window`` and drives the player.

    ``player_bar`` is the window's ``PlayerBar``: the keys that do what one of
    its buttons does go through it, so the buttons always show the truth.
    """

    def __init__(self, window, playback_manager, player_bar):
        self.window = window
        self.playback_manager = playback_manager
        self.player_bar = player_bar

        # (key, modifiers) -> (what it does, whether holding the key repeats
        # it, whether it works while typing).
        self._bindings = {}
        # The keys (hardware codes) that are down right now. A key that is
        # held down sends the same press again and again: that is wanted for
        # a seek or a volume change, but not for pausing, which would
        # flicker.
        self._held = set()

        left = (Gdk.KEY_Left, Gdk.KEY_KP_Left)
        right = (Gdk.KEY_Right, Gdk.KEY_KP_Right)
        up = (Gdk.KEY_Up, Gdk.KEY_KP_Up)
        down = (Gdk.KEY_Down, Gdk.KEY_KP_Down)

        self._bind((Gdk.KEY_space,), _NONE, player_bar.press_play)
        self._bind(left, _NONE, lambda: self._seek(-SEEK_STEP), repeat=True)
        self._bind(right, _NONE, lambda: self._seek(SEEK_STEP), repeat=True)
        self._bind(up, _NONE, lambda: self._change_volume(VOLUME_STEP), repeat=True)
        self._bind(down, _NONE, lambda: self._change_volume(-VOLUME_STEP), repeat=True)
        self._bind(left, _CTRL, playback_manager.previous)
        self._bind(right, _CTRL, playback_manager.next)
        self._bind((Gdk.KEY_m,), _NONE, player_bar.toggle_mute)
        self._bind((Gdk.KEY_s,), _NONE, player_bar.toggle_shuffle)
        self._bind((Gdk.KEY_r,), _NONE, player_bar.cycle_repeat)

        # The keyboard's media keys.
        self._bind((Gdk.KEY_AudioPlay,), _NONE, player_bar.press_play, typing=True)
        self._bind((Gdk.KEY_AudioPause,), _NONE, playback_manager.pause, typing=True)
        self._bind((Gdk.KEY_AudioStop,), _NONE, playback_manager.stop, typing=True)
        self._bind((Gdk.KEY_AudioNext,), _NONE, playback_manager.next, typing=True)
        self._bind((Gdk.KEY_AudioPrev,), _NONE, playback_manager.previous, typing=True)

        # Before the focused widget, on the way down to it.
        controller = Gtk.EventControllerKey.new()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        controller.connect("key-released", self._on_key_released)
        window.add_controller(controller)

        # A release that happens while another window has the focus never
        # reaches this one: forget the keys that were down.
        window.connect("notify::is-active", lambda *_args: self._held.clear())

    def _bind(self, keys, modifiers, action, *, repeat=False, typing=False):
        """Make ``action()`` run when one of ``keys`` is pressed with exactly
        ``modifiers`` held.

        ``repeat``: holding the key runs it again and again.
        ``typing``: it also works while the focus is in a text field, a menu
        or a dialog (the media keys: they cannot be typed).
        """
        for key in keys:
            self._bindings[(key, modifiers)] = (action, repeat, typing)

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def _on_key_pressed(self, _controller, keyval, keycode, state):
        # A letter is the same key with Caps Lock on or Shift held.
        binding = self._bindings.get(
            (Gdk.keyval_to_lower(keyval), int(state) & _MODIFIERS)
        )

        if binding is None:
            return False

        action, repeat, typing = binding

        if not typing and self._should_leave_alone():
            return False

        if repeat or keycode not in self._held:
            action()

        self._held.add(keycode)
        return True

    def _on_key_released(self, _controller, _keyval, keycode, _state):
        self._held.discard(keycode)

    def _should_leave_alone(self):
        """Whether the key is for something other than the player: text being
        typed, a menu being used, or a dialog on top of the window."""
        # A dialog that lives inside the window (libadwaita 1.5 and later).
        get_visible_dialog = getattr(self.window, "get_visible_dialog", None)

        if get_visible_dialog is not None and get_visible_dialog() is not None:
            return True

        widget = self.window.get_focus()

        while widget is not None:
            if isinstance(widget, (Gtk.Editable, Gtk.TextView, Gtk.Popover)):
                return True

            widget = widget.get_parent()

        return False

    # ------------------------------------------------------------------
    # What they do
    # ------------------------------------------------------------------

    def _seek(self, offset):
        """Go ``offset`` seconds forward (or back, if it is negative) in the
        track being played."""
        manager = self.playback_manager

        if manager.current_track is None:
            return

        target = manager.get_clip_position() + offset
        duration = manager.get_clip_duration()

        # Past the end of the track is the same as asking for the next one
        # (which is what MPRIS's Seek does too).
        if duration > 0 and target >= duration:
            manager.next()
            return

        manager.seek_clip(max(0.0, target))

    def _change_volume(self, step):
        """Raise or lower Melody's volume. Done through the player bar's
        slider, which sets the player's volume and shows it."""
        volume = self.playback_manager.volume + step
        self.player_bar.set_volume(round(max(0.0, min(1.0, volume)), 2))
