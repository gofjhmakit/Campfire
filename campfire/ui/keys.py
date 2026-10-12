"""Configurable key bindings (the [keys] table of config.toml).

A binding maps an action to one key or a list of keys. A key is a single
character ("n", "G", "1", "/") or one of the names in SPECIAL ("space",
"left", "f5", ...). The arrow keys up/down, Page Up/Down, Home/End, Enter, Esc
and Backspace always keep their navigation meaning and cannot be rebound.
"""

from __future__ import annotations

import curses

# action: (default keys, description shown in the help screen)
ACTIONS: dict[str, tuple[list[str], str]] = {
    "play_pause": (["space"], "Play / pause"),
    "seek_back": (["left"], "Seek back"),
    "seek_forward": (["right"], "Seek forward"),
    "next": (["n"], "Next track"),
    "previous": (["p"], "Previous track"),
    "stop": (["s"], "Stop"),
    "volume_up": (["+", "="], "Volume up"),
    "volume_down": (["-"], "Volume down"),
    "goto_playing": (["."], "Go to playing album"),
    "artists": (["1"], "Artists"),
    "albums": (["2"], "Albums"),
    "tracks": (["3"], "Tracks"),
    "queue": (["4"], "Queue"),
    "search": (["/"], "Search"),
    "refresh": (["r"], "Refresh library"),
    "help": (["?"], "This help"),
    "quit": (["q"], "Quit"),
    "move_up": (["k"], "Move up"),
    "move_down": (["j"], "Move down"),
    "top": (["g"], "Top"),
    "bottom": (["G"], "Bottom"),
    "back": (["h"], "Back"),
}

SPECIAL: dict[str, str | int] = {
    "space": " ", "tab": "\t",
    "left": curses.KEY_LEFT, "right": curses.KEY_RIGHT,
    "insert": curses.KEY_IC, "delete": curses.KEY_DC,
    **{f"f{n}": curses.KEY_F0 + n for n in range(1, 13)},
}

DISPLAY = {"space": "space", "tab": "tab", "left": "←", "right": "→",
           "insert": "ins", "delete": "del"}

# Keys with a fixed navigation meaning.
RESERVED = {"up", "down", "pageup", "pagedown", "home", "end", "enter", "esc", "escape",
            "backspace", "return"}


def parse_key(name: str) -> str | int:
    """Turn a configured key name into what curses get_wch() returns."""
    if not isinstance(name, str) or not name:
        raise ValueError("key must be a non-empty string")
    if len(name) == 1:
        if not name.isprintable():
            raise ValueError(f"unsupported key {name!r}")
        return name
    lowered = name.lower()
    if lowered in RESERVED:
        raise ValueError(f"'{name}' is reserved for navigation")
    if lowered in SPECIAL:
        return SPECIAL[lowered]
    raise ValueError(f"unknown key name '{name}'")


def display(name: str) -> str:
    return DISPLAY.get(name.lower(), name.upper() if name.lower().startswith("f") and len(name) > 1 else name)


class Keymap:
    def __init__(self, user: dict | None = None):
        self.warnings: list[str] = []
        self.names: dict[str, list[str]] = {}       # action -> key names
        self._actions: dict[str | int, str] = {}    # key code -> action

        user = user or {}
        for action, value in user.items():
            if action not in ACTIONS:
                self.warnings.append(f"keys.{action}: unknown action; ignored.")
                continue
            names = [value] if isinstance(value, str) else value
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                self.warnings.append(f"keys.{action}: expected a key or a list of keys; using the default.")
                continue
            self._bind(action, names, user=True)

        for action, (defaults, _) in ACTIONS.items():
            if action not in self.names:
                self._bind(action, defaults, user=False)

    def _bind(self, action: str, names: list[str], user: bool) -> None:
        bound = []
        for name in names:
            try:
                code = parse_key(name)
            except ValueError as exc:
                self.warnings.append(f"keys.{action}: {exc}.")
                continue
            owner = self._actions.get(code)
            if owner == action:
                continue  # listed twice
            if owner is not None:
                if user:
                    self.warnings.append(f"keys.{action}: '{name}' is already bound to {owner}; ignored.")
                continue
            self._actions[code] = action
            bound.append(name)
        if user and not bound:
            self.warnings.append(f"keys.{action}: no usable keys; using the default.")
            self._bind(action, ACTIONS[action][0], user=False)
            return
        self.names[action] = bound
        if not user and not bound:
            self.warnings.append(f"No key left for '{action}' (its default is used by another action).")

    def action(self, key: str | int) -> str | None:
        return self._actions.get(key)

    def label(self, action: str) -> str:
        """Display form of an action's first key, e.g. '1', 'space', '←'."""
        names = self.names.get(action) or []
        return display(names[0]) if names else ""

    def labels(self, action: str) -> str:
        return "  ".join(display(n) for n in self.names.get(action) or [])
