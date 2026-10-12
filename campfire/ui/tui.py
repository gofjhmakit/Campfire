"""Curses user interface: layout, drawing, keyboard handling and the main loop."""

from __future__ import annotations

import curses
import locale
import os
import select
import signal
import sys
import time

from .. import APP_TITLE, credentials
from ..app import App
from ..config import load_settings, themes_dir
from ..models import format_duration
from ..playback.controller import Status
from ..theme import load_theme
from . import text as tx
from .colors import Palette
from .keys import Keymap
from .views import (AlbumsView, AlbumView, ArtistsView, QueueView, Row, SearchView,
                    TracksView, View)

MIN_W, MIN_H = 40, 10
SIDEBAR_MIN_WIDTH = 84
SIDEBAR_W = 16
MAX_FPS = 20

ENTER = {"\n", "\r", curses.KEY_ENTER}
BACKSPACE = {"\x7f", "\b", curses.KEY_BACKSPACE}
ESC = "\x1b"

UNICODE_SYMBOLS = {
    "playing": "▶", "paused": "⏸", "stopped": "■", "loading": "…", "marker": "▶",
    "bar": "━", "bar_empty": "━", "sep": "─", "dot": "·", "cursor": "▏",
    "meter": " ▏▎▍▌▋▊▉█", "arrow": "›",
}
ASCII_SYMBOLS = {
    "playing": ">", "paused": "=", "stopped": "#", "loading": "~", "marker": ">",
    "bar": "=", "bar_empty": "-", "sep": "-", "dot": "-", "cursor": "_",
    "meter": " ........#", "arrow": ">",
}

# Sidebar / tab sections: (view root, label). The root name is also the keymap action.
NAV = [("artists", "Artists"), ("albums", "Albums"), ("tracks", "Tracks"),
       ("queue", "Queue"), ("search", "Search")]
SECTIONS = ("artists", "albums", "tracks", "queue")

# Help screen: sections of (actions or a fixed key label, description).
HELP = [
    ("Playback", [(("play_pause",), "Play / pause"), (("seek_back", "seek_forward"), "Seek back / forward"),
                  (("next", "previous"), "Next / previous track"), (("stop",), "Stop"),
                  (("volume_up", "volume_down"), "Volume up / down"),
                  (("goto_playing",), "Go to playing album")]),
    ("Navigation", [(("move_up", "move_down"), "Move (also ↑ ↓)"), ("PgUp PgDn", "Page"),
                    (("top", "bottom"), "Top / bottom"), ("Enter", "Open or play"),
                    (("back",), "Back (also Esc ⌫)")]),
    ("Library", [(("artists",), "Artists"), (("albums",), "Albums"), (("tracks",), "Tracks"),
                 (("queue",), "Queue"), (("search",), "Search"), (("refresh",), "Refresh library")]),
    ("General", [(("help",), "This help"), (("quit",), "Quit")]),
]


class Tui:
    def __init__(self, stdscr: curses.window, app: App):
        self.scr = stdscr
        self.app = app
        self.pal = Palette(app.theme)
        self.keymap = Keymap(app.settings.keys)
        for warning in self.keymap.warnings:
            app.notify("warning", warning)
        utf8 = "utf" in (locale.getpreferredencoding(False) or "").lower()
        self.sym = UNICODE_SYMBOLS if utf8 else ASCII_SYMBOLS
        self.roots: dict[str, View] = {"artists": ArtistsView(), "albums": AlbumsView(),
                                       "tracks": TracksView(), "queue": QueueView()}
        self.stack: list[View] = [self.roots["albums"]]
        self.show_help = False
        self.quit = False
        self.resized = False
        self.levels: list[float] = []
        self._last_draw = 0.0
        self._last_levels = 0.0
        self._page = 10
        self._mpv_missing = not app.mpv_available

    # -- Properties ------------------------------------------------------------

    @property
    def view(self) -> View:
        return self.stack[-1]

    @property
    def searching(self) -> bool:
        return isinstance(self.view, SearchView)

    # -- Main loop -------------------------------------------------------------------

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.keypad(True)
        self.scr.nodelay(True)
        try:
            curses.set_escdelay(25)
        except AttributeError:
            pass
        self.scr.bkgd(" ", self.pal["text"])
        signal.signal(signal.SIGWINCH, self._on_resize)
        self.app.start()
        dirty = True
        while not self.quit:
            if self.app.process_events():
                dirty = True
            if self.resized:
                self._apply_resize()
                dirty = True
            if self._poll_levels():
                dirty = True
            now = time.monotonic()
            if dirty and now - self._last_draw >= 1 / MAX_FPS:
                self.draw()
                self._last_draw, dirty = now, False
            ready, _, _ = select.select([sys.stdin, self.app.bus], [], [], self._timeout(dirty))
            if sys.stdin in ready:
                dirty |= self._read_keys()
            if not ready:
                dirty = True  # periodic refresh: notices expiring, clock-driven state

    def _timeout(self, dirty: bool) -> float:
        if dirty:
            return 1 / MAX_FPS
        status = self.app.player.status
        if status == Status.PLAYING and self.app.settings.visualizer:
            return 0.1
        if status in (Status.PLAYING, Status.LOADING, Status.BUFFERING):
            return 0.5
        expiry = self.app.next_notice_expiry()
        if expiry is not None:
            return max(0.05, min(5.0, expiry - time.monotonic() + 0.05))
        return 5.0 if not self.app.sync.running else 1.0

    def _read_keys(self) -> bool:
        # Drain everything curses has buffered, not just what select() saw.
        handled = False
        while True:
            try:
                key = self.scr.get_wch()
            except curses.error:
                return handled
            handled = True
            if key == curses.KEY_RESIZE:
                self.resized = True
                continue
            self.handle_key(key)
            if self.quit:
                return True

    def _on_resize(self, signum, frame) -> None:
        self.resized = True
        self.app.bus.wake()

    def _apply_resize(self) -> None:
        self.resized = False
        try:
            size = os.get_terminal_size(sys.__stdout__.fileno())
            curses.resizeterm(size.lines, size.columns)
        except (OSError, curses.error, ValueError):
            pass
        self.scr.clear()

    def _poll_levels(self) -> bool:
        if not self.app.settings.visualizer:
            return False
        if self.app.player.status != Status.PLAYING:
            if self.levels:
                self.levels = []
                return True
            return False
        now = time.monotonic()
        if now - self._last_levels < 0.1:
            return False
        self._last_levels = now
        levels = self.app.player.levels()
        if levels is None:
            if self.levels:
                self.levels = []
                return True
            return False
        # Fast attack, slow release, like a real meter.
        normalized = [max(0.0, min(1.0, (db + 50) / 50)) if db == db else 0.0 for db in levels]
        previous = self.levels or [0.0] * len(normalized)
        self.levels = [max(n, p - 0.08) for n, p in zip(normalized, previous + [0.0] * len(normalized))]
        return True

    # -- Input -------------------------------------------------------------------------

    def handle_key(self, key: str | int) -> None:
        if self.show_help:
            self.show_help = False
            return
        if self.searching and self._search_key(key):
            return
        if self._navigation_key(key):
            return
        action = self.keymap.action(key)
        if action is None:
            return
        player = self.app.player
        handlers = {
            "play_pause": player.toggle_pause,
            "seek_back": lambda: player.seek(-1), "seek_forward": lambda: player.seek(1),
            "next": player.next, "previous": player.previous, "stop": player.stop,
            "volume_up": lambda: player.change_volume(5),
            "volume_down": lambda: player.change_volume(-5),
            "goto_playing": self.goto_playing, "search": self.open_search,
            "refresh": self.app.refresh, "help": self.toggle_help, "quit": self.request_quit,
            "move_up": lambda: self._move(-1), "move_down": lambda: self._move(1),
            "top": lambda: self._move(-10**9), "bottom": lambda: self._move(10**9),
            "back": self.back,
        }
        if action in SECTIONS:
            self.switch_root(action)
        elif action in handlers:
            handlers[action]()

    def _search_key(self, key: str | int) -> bool:
        """Text entry for the search view. Returns True if the key was consumed."""
        view: SearchView = self.view  # type: ignore[assignment]
        if key == ESC:
            self.back()
        elif key in BACKSPACE:
            view.query = view.query[:-1]
            view.cursor = 0
        elif key == "\x15":  # Ctrl+U
            view.query, view.cursor = "", 0
        elif key == "\x17":  # Ctrl+W
            view.query, view.cursor = view.query.rstrip().rpartition(" ")[0], 0
        elif isinstance(key, str) and key.isprintable():
            view.query += key
            view.cursor = 0
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            return True  # don't seek while typing
        else:
            return False
        return True

    def _move(self, delta: int) -> None:
        rows = self.view.rows(self.app)
        self.view.cursor = max(0, min(len(rows) - 1, self.view.cursor + delta))

    def _navigation_key(self, key: str | int) -> bool:
        """Fixed navigation keys that cannot be rebound."""
        moves = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -self._page,
                 curses.KEY_NPAGE: self._page, curses.KEY_HOME: -10**9, curses.KEY_END: 10**9}
        if key in moves:
            self._move(moves[key])
        elif key in ENTER:
            view = self.view
            row = view.selected(self.app)
            if row:
                nxt = view.activate(self.app, row)
                if nxt:
                    self.stack.append(nxt)
        elif key in BACKSPACE or key == ESC:
            self.back()
        else:
            return False
        return True

    def back(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()

    def switch_root(self, name: str) -> None:
        self.stack = [self.roots[name]]

    def open_search(self) -> None:
        if not self.searching:
            self.stack.append(SearchView())

    def goto_playing(self) -> None:
        track = self.app.player.current
        if not track:
            return
        view = AlbumView(track.album_id)
        view.pending_track = track.id
        self.stack = [self.roots["albums"], view]

    def toggle_help(self) -> None:
        self.show_help = not self.show_help

    def request_quit(self) -> None:
        self.quit = True

    # -- Drawing primitives ---------------------------------------------------------------

    def put(self, y: int, x: int, s: str, role: str = "text", cells: int | None = None,
            align: str = "left") -> int:
        """Draw text clipped to the screen; returns the x after the text."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return x
        limit = w - x if cells is None else min(cells, w - x)
        s = tx.fit(tx.clean(s), limit, align) if cells is not None else tx.truncate(tx.clean(s), limit)
        if y == h - 1 and x + tx.width(s) >= w:  # the bottom-right cell cannot be written
            s = tx.truncate(s, w - x - 1, "")
        try:
            self.scr.addstr(y, x, s, self.pal[role])
        except curses.error:
            pass
        return x + tx.width(s)

    def hline(self, y: int, x: int, n: int, role: str = "border") -> None:
        if n > 0:
            self.put(y, x, self.sym["sep"] * n, role)

    # -- Frame -------------------------------------------------------------------------------

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            self.put(h // 2, max(0, (w - 26) // 2), f"Terminal too small ({w}×{h})", "warning")
            self.scr.noutrefresh()
            curses.doupdate()
            return

        hints_h = 1 if h >= 16 else 0
        np_h = 3 if h >= 14 else 2
        status_y = h - 1 - hints_h
        np_y = status_y - np_h
        main_top, main_bottom = 2, np_y - 2  # inclusive bottom row

        self.draw_header(0, w)
        sidebar = w >= SIDEBAR_MIN_WIDTH
        if not sidebar:
            self.draw_tabs(1, 2, w - 2)
            main_top += 1
        content_x = 2 + (SIDEBAR_W + 2 if sidebar else 0)
        if sidebar:
            self.draw_sidebar(main_top, main_bottom, 2)
        self.draw_view(main_top, main_bottom, content_x, w - 2)
        self.hline(np_y - 1, 2, w - 4)
        self.draw_now_playing(np_y, np_h, 2, w - 2)
        self.draw_status(status_y, 2, w - 2)
        if hints_h:
            self.draw_hints(h - 1, 2, w - 2)
        if self.show_help:
            self.draw_help(h, w)
        self.scr.noutrefresh()
        curses.doupdate()

    def draw_header(self, y: int, w: int) -> None:
        x = self.put(y, 2, APP_TITLE, "accent")
        crumbs = [v.title for v in self.stack if v.title]
        if crumbs:
            x = self.put(y, x + 2, f" {self.sym['arrow']} ".join(crumbs), "secondary")
        right = f"{self.keymap.label('search')} search  {self.keymap.label('help')} help"
        if self.app.sync.running:
            right = "syncing…   " + right
        if x + tx.width(right) + 4 < w:
            self.put(y, w - 2 - tx.width(right), right, "muted")

    def draw_sidebar(self, top: int, bottom: int, x: int) -> None:
        active = self.view.root
        y = top
        self.put(y, x, "LIBRARY", "muted")
        y += 1
        for name, label in NAV:
            key = self.keymap.label(name)
            if name == "queue":
                y += 1
                self.put(y, x, "PLAYBACK", "muted")
                y += 1
            elif name == "search":
                y += 1
            if y > bottom:
                return
            is_active = name == active
            self.put(y, x, (self.sym["marker"] if is_active else " "), "accent" if is_active else "muted")
            x_label = self.put(y, x + 2, f"{key}:", "accent" if is_active else "accent_plain") + 1
            self.put(y, x_label, label, "accent" if is_active else "text", max(0, SIDEBAR_W - (x_label - x)))
            y += 1

    def draw_tabs(self, y: int, x0: int, x1: int) -> None:
        """Section tabs for narrow terminals, where the sidebar is hidden."""
        active = self.view.root
        for short in (False, True):
            items = [(self.keymap.label(name), label[:3] if short else label, name) for name, label in NAV]
            total = sum(tx.width(f"{k}: {l}") for k, l, _ in items) + 3 * (len(items) - 1)
            if total <= x1 - x0:
                break
            items = [i for i in items if i[2] != "search"]  # search is in the hints anyway
            total = sum(tx.width(f"{k}: {l}") for k, l, _ in items) + 3 * (len(items) - 1)
            if total <= x1 - x0:
                break
        x = x0
        for key, label, name in items:
            is_active = name == active
            x = self.put(y, x, f"{key}:", "accent" if is_active else "accent_plain")
            x = self.put(y, x + 1, label, "accent" if is_active else "muted") + 3

    def draw_view(self, top: int, bottom: int, x0: int, x1: int) -> None:
        view = self.view
        width = x1 - x0
        rows = view.rows(self.app)
        y = top
        if isinstance(view, SearchView):
            x = self.put(y, x0, "/ ", "accent")
            x = self.put(y, x, view.query, "bold")
            self.put(y, x, self.sym["cursor"], "accent")
        pos = f"{view.cursor + 1}/{len(rows)}" if rows else ""
        title_end = x1 - (tx.width(pos) + 2 if pos else 0)
        if not isinstance(view, SearchView):
            x = self.put(y, x0, view.title, "bold", min(tx.width(view.title), title_end - x0))
            if (sub := view.subtitle(self.app)) and x + 2 < title_end:
                self.put(y, x + 2, sub, "secondary", title_end - x - 2)
        if pos:
            self.put(y, x1 - tx.width(pos), pos, "muted")
        y += 2

        widths = tx.column_widths(width - 2, [(c[0], c[1]) for c in view.columns])
        if view.headers and rows and y <= bottom:
            self._draw_cells(y, x0 + 2, view.headers, widths, view.columns, "muted", "muted")
            y += 1

        visible = max(1, bottom - y + 1)
        self._page = max(1, visible - 1)
        if not rows:
            self.put(y, x0 + 2, view.empty_message(self.app), "muted")
            return
        view.cursor = max(0, min(view.cursor, len(rows) - 1))
        if view.cursor < view.scroll:
            view.scroll = view.cursor
        elif view.cursor >= view.scroll + visible:
            view.scroll = view.cursor - visible + 1
        view.scroll = max(0, min(view.scroll, max(0, len(rows) - visible)))

        current = self.app.player.current
        for i in range(view.scroll, min(len(rows), view.scroll + visible)):
            self._draw_row(y, x0, x1, rows[i], i == view.cursor, widths, view.columns, current)
            y += 1

    def _draw_row(self, y: int, x0: int, x1: int, row: Row, selected: bool, widths: list[int],
                  columns, current) -> None:
        playing = current is not None and (
            (row.track_id is not None and row.track_id == current.id) or
            (row.album_id is not None and row.album_id == current.album_id))
        if selected:
            self.put(y, x0, " " * (x1 - x0), "selection")
        if playing:
            self.put(y, x0, self.sym["marker"], "current_selected" if selected else "current")
        elif row.kind == "action":
            self.put(y, x0, self.sym["marker"], "selection" if selected else "accent_plain")
        if selected:
            main, other = ("current_selected", "selection_dim") if playing else ("selection", "selection_dim")
        elif playing:
            main, other = "current", "secondary"
        elif row.kind == "action":
            main, other = "accent_plain", "muted"
        else:
            main, other = "text", "secondary"
        self._draw_cells(y, x0 + 2, row.cells, widths, columns, main, other)

    def _draw_cells(self, y, x, cells, widths, columns, main_role, other_role) -> None:
        # The first flexible column is the item's name; the rest are details.
        main_col = next((i for i, c in enumerate(columns) if c[1]), 0)
        for i, (cell, w) in enumerate(zip(cells, widths)):
            if w <= 0:
                continue
            self.put(y, x, cell, main_role if i == main_col else other_role, w, columns[i][2])
            x += w + 2

    def draw_now_playing(self, y: int, height: int, x0: int, x1: int) -> None:
        player = self.app.player
        track = player.current
        status = player.status
        width = x1 - x0
        icon, icon_role = {
            Status.PLAYING: (self.sym["playing"], "accent"),
            Status.PAUSED: (self.sym["paused"], "accent_plain"),
            Status.LOADING: (self.sym["loading"], "muted"),
            Status.BUFFERING: (self.sym["loading"], "muted"),
            Status.STOPPED: (self.sym["stopped"], "muted"),
        }[status]
        right = f"vol {player.volume}%"
        if status in (Status.PAUSED, Status.LOADING, Status.BUFFERING) or (status == Status.STOPPED and track):
            right = f"{status.value}  {self.sym['dot']}  {right}"

        if not track:
            self.put(y, x0, icon, "muted")
            self.put(y, x0 + 2, "Nothing playing", "muted")
            self.put(y, x1 - tx.width(right), right, "muted")
            if height >= 3:
                if self._mpv_missing:
                    self.put(y + 1, x0 + 2, f"Playback unavailable: mpv not found "
                                            f"('{self.app.settings.mpv_path}').", "warning")
                else:
                    self.put(y + 1, x0 + 2, "Pick an album, artist or track and press Enter.", "muted")
            self._draw_progress(y + height - 1, x0, x1, None, None)
            return

        self.put(y, x0, icon, icon_role)
        title_w = width - 2 - tx.width(right) - 2
        self.put(y, x0 + 2, track.title, "bold", max(0, title_w))
        self.put(y, x1 - tx.width(right), right, "muted")
        if height >= 3:
            meter_w = self._meter_width()
            upcoming = player.queue.upcoming(1)
            nxt = f"next {self.sym['dot']} {upcoming[0].title}" if upcoming else ""
            detail = f"{track.artist} — {track.album}" if track.album else track.artist
            reserved = meter_w + 2 if meter_w else 0
            x = self.put(y + 1, x0 + 2, tx.truncate(detail, width - 2 - reserved), "secondary")
            if nxt and x + 4 + tx.width(nxt) < x1 - reserved:
                self.put(y + 1, x + 4, nxt, "muted")
            if meter_w and width > 50:
                self._draw_meter(y + 1, x1 - meter_w)
        else:
            detail = f"  {track.artist}"
            used = 2 + tx.width(track.title)
            if used + 4 < title_w:
                self.put(y, x0 + used, tx.truncate(detail, title_w - used), "secondary")
        self._draw_progress(y + height - 1, x0, x1, player.position, player.duration)

    def _draw_progress(self, y: int, x0: int, x1: int, position, duration) -> None:
        times = f"{format_duration(position)} / {format_duration(duration)}"
        bar_w = (x1 - x0) - tx.width(times) - 4
        if bar_w < 5:
            self.put(y, x0 + 2, times, "secondary")
            return
        fraction = 0.0
        if position is not None and duration:
            fraction = max(0.0, min(1.0, position / duration))
        filled = int(round(fraction * bar_w))
        self.put(y, x0 + 2, self.sym["bar"] * filled, "progress")
        self.put(y, x0 + 2 + filled, self.sym["bar_empty"] * (bar_w - filled), "progress_empty")
        self.put(y, x1 - tx.width(times), times, "secondary")

    METER_CELLS = 10

    def _meter_width(self) -> int:
        n = len(self.levels)
        return n * (self.METER_CELLS + 1) - 1 if n else 0

    def _draw_meter(self, y: int, x: int) -> None:
        steps = self.sym["meter"]
        for level in self.levels:
            cells = level * self.METER_CELLS
            full = int(cells)
            partial = int((cells - full) * (len(steps) - 1))
            bar = steps[-1] * full + (steps[partial] if full < self.METER_CELLS else "")
            bar = bar[:self.METER_CELLS]
            self.put(y, x, bar, "progress")
            self.put(y, x + len(bar), steps[-1] * (self.METER_CELLS - len(bar)), "progress_empty")
            x += self.METER_CELLS + 1

    def draw_status(self, y: int, x0: int, x1: int) -> None:
        notice = self.app.current_notice()
        if notice:
            role = {"error": "error", "warning": "warning"}.get(notice.level, "secondary")
            more = self.app.active_notice_count() - 1
            suffix = f"  (+{more} more)" if more > 0 else ""
            x = self.put(y, x0, tx.truncate(notice.text, x1 - x0 - tx.width(suffix)), role)
            if suffix:
                self.put(y, x, suffix, "muted")
        elif self.app.sync.running:
            s = self.app.sync
            msg = (f"Loading tracks {s.done}/{s.total} albums" if s.phase == "tracks" and s.total
                   else "Loading collection…")
            self.put(y, x0, msg, "muted")

    def draw_hints(self, y: int, x0: int, x1: int) -> None:
        if self.searching:
            hints = [("type", "search"), ("↑↓", "select"), ("enter", "open"), ("esc", "back"),
                     ("^U", "clear")]
        else:
            k = self.keymap.label
            sections = [k(a) for a in SECTIONS]
            if sections == ["1", "2", "3", "4"]:
                section_keys = "1-4"
            else:
                section_keys = " ".join(sections)
            def pair(a: str, b: str, joiner: str = "/") -> str:
                return joiner.join(x for x in (k(a), k(b)) if x)
            seek = pair("seek_back", "seek_forward", "" if (k("seek_back"), k("seek_forward")) == ("←", "→") else "/")
            hints = [(k("play_pause"), "play/pause"), (seek, "seek"),
                     (pair("next", "previous"), "next/prev"), (section_keys.strip(), "sections"),
                     (k("search"), "search"), (k("help"), "help"), (k("quit"), "quit")]
            hints = [(key, label) for key, label in hints if key]  # actions left without a key
        x = x0
        for key, label in hints:
            need = tx.width(key) + 1 + tx.width(label)
            if x + need > x1:
                break
            x = self.put(y, x, key, "accent_plain")
            x = self.put(y, x + 1, label, "muted") + 3

    def _help_keys(self, keys: tuple[str, ...] | str) -> str:
        if isinstance(keys, str):
            return keys
        return "  ".join(label for label in (self.keymap.label(a) for a in keys) if label)

    def draw_help(self, h: int, w: int) -> None:
        def section_lines(sections):
            lines: list[tuple[str, str, str]] = []
            for title, entries in sections:
                if lines:
                    lines.append(("blank", "", ""))
                lines.append(("section", title, ""))
                lines.extend(("key", self._help_keys(k), d) for k, d in entries)
            return lines

        col_w = 38
        two_columns = w >= 2 * col_w + 8
        if two_columns:
            half = (len(HELP) + 1) // 2
            columns = [section_lines(HELP[:half]), section_lines(HELP[half:])]
        else:
            columns = [section_lines(HELP)]
        box_w = min(w - 4, col_w * len(columns) + 6)
        box_h = min(h - 2, max(len(c) for c in columns) + 5)
        top, left = (h - box_h) // 2, (w - box_w) // 2
        for row in range(box_h):
            self.put(top + row, left, " " * box_w, "selection_dim")
        self.put(top + 1, left + 3, "Keyboard shortcuts", "selection")
        hint = "any key closes"
        self.put(top + 1, left + box_w - 3 - tx.width(hint), hint, "selection_dim")
        for c, lines in enumerate(columns):
            x = left + 3 + c * col_w
            for i, (kind, a, b) in enumerate(lines[: box_h - 4]):
                y = top + 3 + i
                if kind == "section":
                    self.put(y, x, a.upper(), "selection_dim")
                elif kind == "key":
                    self.put(y, x + 2, a, "selection", 11)
                    self.put(y, x + 14, b, "selection_dim", col_w - 15)


def run() -> int:
    """Start the full-screen player. Returns a process exit code."""
    settings, warnings = load_settings()
    theme, theme_warnings = load_theme(settings.theme, themes_dir())
    creds = credentials.load()
    if creds is None:
        print("No Bandcamp Subsonic credentials found.\n\n"
              "  1. Open https://bandcamp.com/settings?pane=fan and generate credentials\n"
              "     in the Subsonic section.\n"
              "  2. Run: campfire login", file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("campfire needs an interactive terminal.", file=sys.stderr)
        return 1

    locale.setlocale(locale.LC_ALL, "")
    app = App(settings, theme, creds, warnings + theme_warnings)

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        curses.wrapper(lambda scr: Tui(scr, app).run())
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        for s, handler in previous.items():
            signal.signal(s, handler)
    return 0
