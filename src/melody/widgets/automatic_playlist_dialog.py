"""The window to create or change an automatic playlist: its name and the
rules that pick its tracks (see ``melody.services.automatic_playlists``).

A live line at the bottom says how many tracks the rules select right now, so
the rules can be tried before saving.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from melody.services.automatic_playlists import (
    DATE_UNIT,
    FIELDS,
    OPERATORS,
    SORT_KEYS,
    default_rules,
    evaluate_rules,
    normalize_rules,
)
from melody.services.collection_actions import _close_on_escape

_FIELD_KEYS = list(FIELDS)
_SORT_KEY_LIST = list(SORT_KEYS)

# A new condition starts as this one: the most common thing to ask for.
_NEW_CONDITION = {"field": "play_count", "op": "at_least", "value": 1}


class _ConditionRow(Gtk.Box):
    """One condition: a field, how to compare it, and what to compare it to."""

    def __init__(self, condition, on_changed, on_remove):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._on_changed = on_changed
        self._loading = True
        self._operator_keys = []

        self.field_dropdown = Gtk.DropDown.new_from_strings(
            [FIELDS[key][0] for key in _FIELD_KEYS]
        )
        self.field_dropdown.set_size_request(150, -1)

        self.operator_dropdown = Gtk.DropDown.new_from_strings([""])
        self.operator_dropdown.set_size_request(150, -1)

        self.text_entry = Gtk.Entry()
        self.text_entry.set_hexpand(True)

        self.number_spin = Gtk.SpinButton.new_with_range(0, 1000000, 1)
        self.number_spin.set_numeric(True)
        self.number_spin.set_hexpand(True)

        self.flag_dropdown = Gtk.DropDown.new_from_strings(["yes", "no"])
        self.flag_dropdown.set_hexpand(True)

        # Which of the three takes the value depends on the field.
        self.value_stack = Gtk.Stack()
        self.value_stack.set_hexpand(True)
        self.value_stack.add_named(self.text_entry, "text")
        self.value_stack.add_named(self.number_spin, "number")
        self.value_stack.add_named(self.flag_dropdown, "flag")

        self.unit_label = Gtk.Label(label=DATE_UNIT)
        self.unit_label.add_css_class("dim-label")

        remove_button = Gtk.Button.new_from_icon_name("list-remove-symbolic")
        remove_button.add_css_class("flat")
        remove_button.set_tooltip_text("Remove this condition")
        remove_button.connect("clicked", lambda _button: on_remove(self))

        self.append(self.field_dropdown)
        self.append(self.operator_dropdown)
        self.append(self.value_stack)
        self.append(self.unit_label)
        self.append(remove_button)

        self.field_dropdown.connect(
            "notify::selected",
            self._on_field_changed,
        )
        self.operator_dropdown.connect("notify::selected", self._changed)
        self.flag_dropdown.connect("notify::selected", self._changed)
        self.text_entry.connect("changed", self._changed)
        self.number_spin.connect("value-changed", self._changed)

        self._load(condition)
        self._loading = False

    def _changed(self, *_args):
        if not self._loading:
            self._on_changed()

    def _field_key(self):
        return _FIELD_KEYS[self.field_dropdown.get_selected()]

    def _on_field_changed(self, *_args):
        self._show_field(self._field_key())
        self._changed()

    def _show_field(self, key):
        """Offer the operators and the kind of value ``key`` takes."""
        kind = FIELDS[key][1]

        self._operator_keys = list(OPERATORS[kind])
        self.operator_dropdown.set_model(Gtk.StringList.new(
            [OPERATORS[kind][operator] for operator in self._operator_keys]
        ))
        self.operator_dropdown.set_selected(0)

        self.value_stack.set_visible_child_name(
            "number" if kind in ("number", "date") else kind
        )
        self.unit_label.set_visible(kind == "date")

    def _load(self, condition):
        key = condition["field"]
        kind = FIELDS[key][1]

        self.field_dropdown.set_selected(_FIELD_KEYS.index(key))
        self._show_field(key)
        self.operator_dropdown.set_selected(
            self._operator_keys.index(condition["op"])
        )

        value = condition["value"]

        if kind == "text":
            self.text_entry.set_text(str(value))
        elif kind == "flag":
            self.flag_dropdown.set_selected(0 if value else 1)
        else:
            self.number_spin.set_value(float(value))

    def get_condition(self):
        key = self._field_key()
        kind = FIELDS[key][1]

        if kind == "text":
            value = self.text_entry.get_text()
        elif kind == "flag":
            value = self.flag_dropdown.get_selected() == 0
        else:
            value = self.number_spin.get_value()

        return {
            "field": key,
            "op": self._operator_keys[self.operator_dropdown.get_selected()],
            "value": value,
        }


def _caption(text):
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class("caption-heading")
    return label


def show_automatic_playlist_dialog(parent, playlist, library_tracks, on_save):
    """Open the window for an automatic playlist.

    ``playlist`` is the playlist to change, or None to make a new one.
    ``library_tracks`` are what the live count is worked out from.
    ``on_save(name, rules)`` is called with the checked name and rules when
    the person saves.
    """
    editing = playlist is not None
    rules = playlist["rules"] if editing else default_rules()

    window = Adw.Window()
    window.set_transient_for(parent)
    window.set_modal(True)
    window.set_title(
        "Edit automatic playlist" if editing else "New automatic playlist"
    )
    window.set_default_size(600, 700)
    _close_on_escape(window)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)

    cancel_button = Gtk.Button(label="Cancel")
    save_button = Gtk.Button(label="Save")
    save_button.add_css_class("suggested-action")
    header.pack_start(cancel_button)
    header.pack_end(save_button)
    toolbar.add_top_bar(header)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    content.set_margin_start(24)
    content.set_margin_end(24)
    content.set_margin_top(16)
    content.set_margin_bottom(24)

    # Name.
    name_entry = Gtk.Entry()
    name_entry.set_placeholder_text("Playlist name")
    name_entry.set_text(playlist["name"] if editing else "")

    name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    name_box.append(_caption("Name"))
    name_box.append(name_entry)

    # Conditions.
    match_dropdown = Gtk.DropDown.new_from_strings(["all", "any"])
    match_dropdown.set_selected(0 if rules["match"] == "all" else 1)

    match_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    match_row.append(Gtk.Label(label="Include tracks that match"))
    match_row.append(match_dropdown)
    match_row.append(Gtk.Label(label="of these conditions:"))

    conditions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

    empty_hint = Gtk.Label(
        label="No condition: every track is included.",
        xalign=0,
    )
    empty_hint.add_css_class("dim-label")

    add_button = Gtk.Button(label="Add condition")
    add_button.set_halign(Gtk.Align.START)

    conditions_section = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
    )
    conditions_section.append(_caption("Conditions"))
    conditions_section.append(match_row)
    conditions_section.append(conditions_box)
    conditions_section.append(empty_hint)
    conditions_section.append(add_button)

    # Order and limit.
    sort_dropdown = Gtk.DropDown.new_from_strings(
        [SORT_KEYS[key] for key in _SORT_KEY_LIST]
    )
    sort_dropdown.set_selected(_SORT_KEY_LIST.index(rules["sort"]["key"]))

    order_dropdown = Gtk.DropDown.new_from_strings(
        ["ascending", "descending"]
    )
    order_dropdown.set_selected(1 if rules["sort"]["descending"] else 0)

    sort_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    sort_row.append(Gtk.Label(label="Sort by"))
    sort_row.append(sort_dropdown)
    sort_row.append(order_dropdown)

    limit_check = Gtk.CheckButton(label="Limit to")
    limit_check.set_active(rules["limit"] is not None)

    limit_spin = Gtk.SpinButton.new_with_range(1, 100000, 1)
    limit_spin.set_numeric(True)
    limit_spin.set_value(rules["limit"] if rules["limit"] is not None else 50)
    limit_spin.set_sensitive(rules["limit"] is not None)

    limit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    limit_row.append(limit_check)
    limit_row.append(limit_spin)
    limit_row.append(Gtk.Label(label="tracks"))

    order_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    order_section.append(_caption("Order"))
    order_section.append(sort_row)
    order_section.append(limit_row)

    # What the rules select right now, and any problem with them.
    preview_label = Gtk.Label(xalign=0)
    preview_label.add_css_class("dim-label")

    error_label = Gtk.Label(xalign=0)
    error_label.set_wrap(True)
    error_label.add_css_class("error")
    error_label.set_visible(False)

    content.append(name_box)
    content.append(conditions_section)
    content.append(order_section)
    content.append(preview_label)
    content.append(error_label)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    scrolled.set_child(content)

    toolbar.set_content(scrolled)
    window.set_content(toolbar)

    rows = []
    preview_source = [0]

    def collect():
        return {
            "match": "all" if match_dropdown.get_selected() == 0 else "any",
            "conditions": [row.get_condition() for row in rows],
            "sort": {
                "key": _SORT_KEY_LIST[sort_dropdown.get_selected()],
                "descending": order_dropdown.get_selected() == 1,
            },
            "limit": (
                int(limit_spin.get_value()) if limit_check.get_active() else None
            ),
        }

    def run_preview():
        preview_source[0] = 0

        try:
            count = len(evaluate_rules(collect(), library_tracks))
        except ValueError:
            preview_label.set_text("")
            return GLib.SOURCE_REMOVE

        preview_label.set_text(
            f"{count} track{'s' if count != 1 else ''} match right now"
        )
        return GLib.SOURCE_REMOVE

    def schedule_preview(*_args):
        # Wait for typing to pause: working it out for every key press would
        # be wasted on a big library.
        error_label.set_visible(False)

        if preview_source[0]:
            GLib.source_remove(preview_source[0])

        preview_source[0] = GLib.timeout_add(250, run_preview)

    def update_hint():
        empty_hint.set_visible(not rows)

    def remove_condition(row):
        rows.remove(row)
        conditions_box.remove(row)
        update_hint()
        schedule_preview()

    def add_condition(condition=None):
        row = _ConditionRow(
            condition or dict(_NEW_CONDITION),
            schedule_preview,
            remove_condition,
        )
        rows.append(row)
        conditions_box.append(row)
        update_hint()
        schedule_preview()

    for condition in rules["conditions"]:
        add_condition(condition)

    update_hint()
    run_preview()

    add_button.connect("clicked", lambda _button: add_condition())
    match_dropdown.connect("notify::selected", schedule_preview)
    sort_dropdown.connect("notify::selected", schedule_preview)
    order_dropdown.connect("notify::selected", schedule_preview)
    limit_spin.connect("value-changed", schedule_preview)
    name_entry.connect("changed", lambda _entry: error_label.set_visible(False))

    def on_limit_toggled(button):
        limit_spin.set_sensitive(button.get_active())
        schedule_preview()

    limit_check.connect("toggled", on_limit_toggled)

    def show_error(message):
        error_label.set_text(message)
        error_label.set_visible(True)

    def on_save_clicked(_button):
        name = name_entry.get_text().strip()

        if not name:
            show_error("Choose a name for the playlist.")
            name_entry.grab_focus()
            return

        try:
            checked = normalize_rules(collect())
        except ValueError as error:
            show_error(str(error))
            return

        window.close()
        on_save(name, checked)

    def on_close(_window):
        if preview_source[0]:
            GLib.source_remove(preview_source[0])
            preview_source[0] = 0

        return False

    cancel_button.connect("clicked", lambda _button: window.close())
    save_button.connect("clicked", on_save_clicked)
    window.connect("close-request", on_close)

    window.present()

    if not editing:
        name_entry.grab_focus()
