"""Map theme RGB colors onto what the terminal supports (256 or 8 colors)."""

from __future__ import annotations

import curses

from ..theme import Color, Theme

# The xterm 256-color cube levels.
_CUBE = (0, 95, 135, 175, 215, 255)

_BASIC = {  # approximate RGB of the 8 standard colors
    curses.COLOR_BLACK: (0, 0, 0), curses.COLOR_RED: (205, 0, 0),
    curses.COLOR_GREEN: (0, 205, 0), curses.COLOR_YELLOW: (205, 205, 0),
    curses.COLOR_BLUE: (0, 0, 238), curses.COLOR_MAGENTA: (205, 0, 205),
    curses.COLOR_CYAN: (0, 205, 205), curses.COLOR_WHITE: (229, 229, 229),
}


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    # Weighted RGB distance, a cheap approximation of perceived difference.
    return 2 * (a[0] - b[0]) ** 2 + 4 * (a[1] - b[1]) ** 2 + 3 * (a[2] - b[2]) ** 2


def rgb_to_256(rgb: tuple[int, int, int]) -> int:
    """Nearest xterm-256 index, avoiding 0-15 (those are user-configurable)."""
    def nearest_level(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))
    r, g, b = (nearest_level(v) for v in rgb)
    cube_index = 16 + 36 * r + 6 * g + b
    cube_rgb = (_CUBE[r], _CUBE[g], _CUBE[b])
    gray_level = max(0, min(23, round((sum(rgb) / 3 - 8) / 10)))
    gray_rgb = (8 + 10 * gray_level,) * 3
    return cube_index if _dist(rgb, cube_rgb) <= _dist(rgb, gray_rgb) else 232 + gray_level


def rgb_to_8(rgb: tuple[int, int, int]) -> int:
    return min(_BASIC, key=lambda c: _dist(rgb, _BASIC[c]))


class Palette:
    """curses attributes for each UI role."""

    # role: (foreground theme color, background theme color, extra attributes)
    PAIRS = {
        "text": ("text", "background", 0),
        "bold": ("text", "background", curses.A_BOLD),
        "secondary": ("text_secondary", "background", 0),
        "muted": ("muted", "background", 0),
        "accent": ("accent", "background", curses.A_BOLD),
        "accent_plain": ("accent", "background", 0),
        "selection": ("selection_fg", "selection_bg", curses.A_BOLD),
        "selection_dim": ("text_secondary", "selection_bg", 0),
        "current": ("current_track", "background", curses.A_BOLD),
        "current_selected": ("current_track", "selection_bg", curses.A_BOLD),
        "progress": ("progress_fill", "background", 0),
        "progress_empty": ("progress_empty", "background", 0),
        "border": ("border", "background", 0),
        "error": ("error", "background", curses.A_BOLD),
        "warning": ("warning", "background", 0),
    }

    def __init__(self, theme: Theme):
        self.attrs: dict[str, int] = {}
        self.colors = 0
        self.theme = theme
        self._pair_ids: dict[tuple[int, int], int] = {}
        self._default_ok = False
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                default_ok = True
            except curses.error:
                default_ok = False
            self.colors = curses.COLORS
            self._default_ok = default_ok
            self._init_pairs(theme, default_ok)
        else:
            self._monochrome()

    def _convert(self, color: Color, default_ok: bool, fallback: int) -> int:
        if color is None:
            return -1 if default_ok else fallback
        if self.colors >= 256:
            return rgb_to_256(color)
        return rgb_to_8(color)

    def _init_pairs(self, theme: Theme, default_ok: bool) -> None:
        pair_ids = self._pair_ids
        for role, (fg_role, bg_role, extra) in self.PAIRS.items():
            fg = self._convert(theme.colors[fg_role], default_ok, curses.COLOR_WHITE)
            bg = self._convert(theme.colors[bg_role], default_ok, curses.COLOR_BLACK)
            if self.colors < 256 and fg == bg:
                fg = curses.COLOR_WHITE if bg != curses.COLOR_WHITE else curses.COLOR_BLACK
            if (fg, bg) not in pair_ids:
                pair_id = len(pair_ids) + 1
                if pair_id >= curses.COLOR_PAIRS:
                    self.attrs[role] = extra
                    continue
                curses.init_pair(pair_id, fg, bg)
                pair_ids[(fg, bg)] = pair_id
            attr = curses.color_pair(pair_ids[(fg, bg)]) | extra
            if self.colors < 256 and role in ("muted", "border", "progress_empty"):
                attr |= curses.A_DIM
            self.attrs[role] = attr

    def _monochrome(self) -> None:
        for role, (_, _, extra) in self.PAIRS.items():
            self.attrs[role] = extra
        self.attrs["selection"] = curses.A_REVERSE
        self.attrs["selection_dim"] = curses.A_REVERSE
        self.attrs["current_selected"] = curses.A_REVERSE | curses.A_BOLD
        self.attrs["muted"] = curses.A_DIM

    def fixed(self, fg_index: int) -> int:
        """Attribute with an exact 256-color foreground index on the theme background."""
        bg = self._convert(self.theme.colors["background"], self._default_ok, curses.COLOR_BLACK)
        return self._pair(fg_index, bg)

    def _pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        pair_id = self._pair_ids.get(key)
        if pair_id is None:
            pair_id = len(self._pair_ids) + 1
            if pair_id >= min(curses.COLOR_PAIRS, 32767):
                return self.attrs.get("text", 0)  # out of pairs: degrade gracefully
            try:
                curses.init_pair(pair_id, fg, bg)
            except (curses.error, ValueError, OverflowError):
                return self.attrs.get("text", 0)
            self._pair_ids[key] = pair_id
        return curses.color_pair(pair_id)

    def rgb(self, fg: Color, bg: Color | str = "background") -> int:
        """Attribute for arbitrary colors (gradients, artwork); bg defaults to the theme's."""
        if not self.colors:
            return 0
        if isinstance(bg, str):
            bg = self.theme.colors[bg]
        return self._pair(self._convert(fg, self._default_ok, curses.COLOR_WHITE),
                          self._convert(bg, self._default_ok, curses.COLOR_BLACK))

    def __getitem__(self, role: str) -> int:
        return self.attrs.get(role, 0)
