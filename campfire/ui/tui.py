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
from . import graphics
from .colors import Palette
from .keys import Keymap
from .views import (AlbumsView, AlbumView, ArtistsView, ArtistView, NowPlayingView, QueueView, Row,
                    SearchView, TracksView, View)
from .visuals import (METER_FLOOR_DB, Dynamics, block_rows, draw_meter, draw_spectrum,
                      normalize)

MIN_W, MIN_H = 40, 10
SIDEBAR_MIN_WIDTH = 84
SIDEBAR_W = 16
MAX_FPS = 20
ANALYSIS_HZ = 20
METER_CELLS = 10
SLOT_PANEL, SLOT_VIEW = 1, 2  # each slot shows its own copy of a cover (own size)

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
       ("queue", "Queue"), ("now_playing", "Now Playing"), ("search", "Search")]
SECTIONS = ("artists", "albums", "tracks", "queue", "now_playing")

# Help screen: sections of (actions or a fixed key label, description).
HELP = [
    ("Playback", [(("play_pause",), "Play / pause"), (("seek_back", "seek_forward"), "Seek back / forward"),
                  (("next", "previous"), "Next / previous track"), (("stop",), "Stop"),
                  (("volume_up", "volume_down"), "Volume up / down"),
                  (("goto_playing",), "Go to playing album"),
                  (("repeat",), "Repeat: off / all / one"),
                  (("shuffle",), "Shuffle mode on / off")]),
    ("Navigation", [(("move_up", "move_down"), "Move (also ↑ ↓)"), ("PgUp PgDn", "Page"),
                    (("top", "bottom"), "Top / bottom"), ("Enter", "Open or play"),
                    (("back",), "Back (also Esc ⌫)")]),
    ("Library", [(("artists",), "Artists"), (("albums",), "Albums"), (("tracks",), "Tracks"),
                 (("queue",), "Queue"), (("now_playing",), "Now Playing"),
                 (("search",), "Search"), (("refresh",), "Refresh library")]),
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
                                       "tracks": TracksView(), "queue": QueueView(),
                                       "now_playing": NowPlayingView()}
        self.stack: list[View] = [self.roots["albums"]]
        self.show_help = False
        self.quit = False
        self.resized = False
        self.spectrum = Dynamics()
        self.meter = Dynamics()
        self._last_draw = 0.0
        self._last_poll = 0.0
        # Artwork: real images where the terminal supports them, text blocks otherwise.
        self.graphics: graphics.KittyImages | None = None
        setting = app.settings.album_art
        if app.art_enabled and setting in ("auto", "graphics") and self.pal.colors >= 256:
            mode, hint = graphics.detect()
            if setting == "graphics" and mode is None:
                mode = "tmux" if os.environ.get("TMUX") else "direct"  # forced by the user
            elif hint:
                app.notify("info", hint)
            if mode:
                self.graphics = graphics.KittyImages(tmux=mode == "tmux")
        self.art_blocks = app.art_enabled and self.graphics is None and setting != "off" \
            and self.sym is UNICODE_SYMBOLS
        self._big_spectrum = False    # the Now Playing view drew its spectrum this frame
        self._block_cache: dict = {}
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
        try:
            self._loop()
        finally:
            if self.graphics:
                self.graphics.close()

    def _loop(self) -> None:
        dirty = True
        while not self.quit:
            if self.app.process_events():
                dirty = True
            if self.resized:
                self._apply_resize()
                dirty = True
            if self._poll_analysis():
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
        if status == Status.PLAYING and self.app.settings.visualizer != "off":
            return 1 / ANALYSIS_HZ
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

    def _poll_analysis(self) -> bool:
        """Fetch measurements from mpv and animate. True if the display changed."""
        if self.app.settings.visualizer == "off":
            return False
        now = time.monotonic()
        dt = now - self._last_poll
        if dt < 1 / ANALYSIS_HZ:
            return False
        self._last_poll = now
        analysis = self.app.player.analysis()
        bands = [normalize(db) for db in analysis.bands] if analysis else None
        levels = [normalize(db, METER_FLOOR_DB, 0.0) for db in analysis.levels] if analysis else None
        visible = self.spectrum.update(bands, dt)
        visible |= self.meter.update(levels, dt)
        return visible or analysis is not None

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
            "repeat": player.cycle_repeat, "shuffle": player.toggle_shuffle,
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

    def put_attr(self, y: int, x: int, s: str, attr: int) -> None:
        """Draw pre-cleaned text with a raw curses attribute (visualizer, artwork)."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def hline(self, y: int, x: int, n: int, role: str = "border") -> None:
        if n > 0:
            self.put(y, x, self.sym["sep"] * n, role)

    # -- Frame -------------------------------------------------------------------------------

    def draw(self) -> None:
        self._big_spectrum = False
        self._draw_frame()
        self.scr.noutrefresh()
        curses.doupdate()

    def _draw_frame(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            self.put(h // 2, max(0, (w - 26) // 2), f"Terminal too small ({w}×{h})", "warning")
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

        if isinstance(view, NowPlayingView):
            self.draw_now_view(view, rows, y, bottom, x0, x1)
            return
        if isinstance(view, AlbumView):
            album = self.app.library.album(view.album_id)
            box = self._art_box(album, bottom - y + 1, x1 - x0, max_rows=10)
            if box:
                art_rows, art_cols = box
                self._draw_art(album, y, x1 - art_cols, art_cols, art_rows, SLOT_VIEW)
                x1 -= art_cols + 3
        self._draw_list(view, rows, y, bottom, x0, x1)

    def _draw_list(self, view: View, rows: list[Row], y: int, bottom: int, x0: int, x1: int) -> None:
        widths = tx.column_widths(x1 - x0 - 2, [(c[0], c[1]) for c in view.columns])
        if view.headers and rows and y <= bottom:
            self._draw_cells(y, x0 + 2, view.headers, widths, view.columns, "muted", "muted")
            y += 1

        visible = max(1, bottom - y + 1)
        self._page = max(1, visible - 1)
        if not rows:
            self.put(y, x0 + 2, view.empty_message(self.app), "muted", max(0, x1 - x0 - 2))
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

    def draw_now_view(self, view: View, rows: list[Row], y: int, bottom: int, x0: int, x1: int) -> None:
        player = self.app.player
        track = player.current
        if track is None:
            self.put(y, x0 + 2, "Nothing playing. Pick an album, artist or track and press Enter.", "muted")
            return
        avail = bottom - y + 1
        spectrum = self.app.settings.visualizer == "spectrum"
        spec_h = max(3, min(12, avail // 3)) if spectrum and avail >= 10 else 0
        top_h = avail - (spec_h + 1 if spec_h else 0)

        album = self.app.library.album(track.album_id)
        text_x = x0 + 2
        box = self._art_box(album, top_h, x1 - x0, max_rows=16)
        if box:
            art_rows, art_cols = box
            self._draw_art(album, y, x0 + 2, art_cols, art_rows, SLOT_VIEW)
            text_x = x0 + 2 + art_cols + 3
        width = x1 - text_x
        line = y
        self.put(line, text_x, track.title, "bold", width)
        self.put(line + 1, text_x, track.artist, "secondary", width)
        year = album.year if album and album.year else track.year
        self.put(line + 2, text_x, track.album + (f"  {self.sym['dot']}  {year}" if year else ""),
                 "secondary", width)
        q = player.queue
        info = f"  {self.sym['dot']}  ".join(
            [f"Track {q.index + 1} of {len(q)}",
             f"{format_duration(player.position)} / {format_duration(player.duration)}"]
            + self._mode_labels())
        self.put(line + 4, text_x, info, "muted", width)
        list_top = line + 6
        if list_top <= y + top_h - 2:
            self._draw_list(view, rows, list_top, y + top_h - 1, text_x - 2, x1)
        if spec_h:
            self._big_spectrum = True
            draw_spectrum(self.put_attr, self.pal.rgb, self.app.theme.colors, self.spectrum,
                          y + top_h + 1, x0 + 2, x1 - x0 - 2, spec_h,
                          self.app.settings.spectrum_style, self.app.settings.spectrum_peaks,
                          ascii_only=self.sym is ASCII_SYMBOLS)

    # -- Artwork ---------------------------------------------------------------------------

    def _art_box(self, album, avail_rows: int, avail_cols: int, max_rows: int) -> tuple[int, int] | None:
        """(rows, cols) for a square cover in the space, or None if it shouldn't be shown."""
        if not (self.graphics or self.art_blocks) or album is None or not album.cover_art:
            return None
        cover = album.cover_art
        if cover in self.app.artwork and self.app.artwork[cover] is None:
            return None  # could not be decoded
        rows = min(max_rows, avail_rows)
        cols = rows * 2  # terminal cells are about twice as tall as wide
        if rows < 4 or avail_cols - cols < 40:
            return None
        return rows, cols

    def _draw_image(self, artwork, slot: int, y: int, x: int, cols: int, rows: int) -> None:
        """Real image: placeholder characters whose foreground color names the image."""
        h, w = self.scr.getmaxyx()
        cols, rows = min(cols, graphics.MAX_CELLS), min(rows, graphics.MAX_CELLS)
        if x + cols > w or y + rows > h:
            return
        image_id = self.graphics.image(artwork.cover_id, artwork.png, slot, cols, rows)
        attr = self.pal.fixed(image_id)
        for r, line in enumerate(self.graphics.placeholder_lines(cols, rows)):
            try:
                self.scr.addstr(y + r, x, line, attr)
            except curses.error:
                pass

    def _draw_art(self, album, y: int, x: int, cols: int, rows: int, slot: int) -> None:
        artwork = self.app.album_artwork(album)
        if artwork is None:  # still loading: a quiet placeholder square
            for r in range(rows):
                self.put(y + r, x, " " * cols, "selection_dim")
            return
        if self.graphics:
            self._draw_image(artwork, slot, y, x, cols, rows)
            return
        key = (artwork.cover_id, cols, rows)
        runs = self._block_cache.get(key)
        if runs is None:
            # Precompute runs of equal color per row: one addstr per run, not per cell.
            runs = []
            for cells in block_rows(artwork.downscale(cols, rows * 2)):
                line: list[list] = []
                for c, (top, bottom) in enumerate(cells):
                    attr = self.pal.rgb(top, bottom)
                    if line and line[-1][2] == attr:
                        line[-1][1] += "▀"
                    else:
                        line.append([c, "▀", attr])
                runs.append(line)
            if len(self._block_cache) > 8:
                self._block_cache.clear()
            self._block_cache[key] = runs
        for r, line in enumerate(runs):
            for offset, text, attr in line:
                self.put_attr(y + r, x + offset, text, attr)

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
        settings = self.app.settings
        in_now_view = isinstance(self.view, NowPlayingView) and self._big_spectrum
        icon, icon_role = {
            Status.PLAYING: (self.sym["playing"], "accent"),
            Status.PAUSED: (self.sym["paused"], "accent_plain"),
            Status.LOADING: (self.sym["loading"], "muted"),
            Status.BUFFERING: (self.sym["loading"], "muted"),
            Status.STOPPED: (self.sym["stopped"], "muted"),
        }[status]
        right = f"  {self.sym['dot']}  ".join(self._mode_labels() + [f"vol {player.volume}%"])
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

        # Mini spectrum on the right (the Now Playing view has a large one instead).
        mini_spectrum = settings.visualizer == "spectrum" and not in_now_view and height >= 3 \
            and x1 - x0 >= 90
        if mini_spectrum:
            spec_w = min(settings.spectrum_bands * 2 - 1, 47)
            draw_spectrum(self.put_attr, self.pal.rgb, self.app.theme.colors, self.spectrum,
                          y, x1 - spec_w, spec_w, height, settings.spectrum_style,
                          settings.spectrum_peaks, ascii_only=self.sym is ASCII_SYMBOLS)
            x1 -= spec_w + 3
        # Cover thumbnail where real images are available.
        if self.graphics and height >= 3 and not isinstance(self.view, NowPlayingView):
            album = self.app.library.album(track.album_id)
            artwork = self.app.album_artwork(album) if album else None
            if artwork:
                self._draw_image(artwork, SLOT_PANEL, y, x0, 6, 3)
                x0 += 8
        width = x1 - x0

        self.put(y, x0, icon, icon_role)
        title_w = width - 2 - tx.width(right) - 2
        self.put(y, x0 + 2, track.title, "bold", max(0, title_w))
        self.put(y, x1 - tx.width(right), right, "muted")
        if height >= 3:
            # The level meter stands in for the spectrum when there's no room for it.
            show_meter = (settings.visualizer == "meter" or
                          (settings.visualizer == "spectrum" and not in_now_view and not mini_spectrum)) \
                and len(self.meter.bars) == 2 and width > 60
            meter_w = 2 * METER_CELLS + 1 if show_meter else 0
            upcoming = player.queue.upcoming(1)
            nxt = f"next {self.sym['dot']} {upcoming[0].title}" if upcoming else ""
            detail = f"{track.artist} — {track.album}" if track.album else track.artist
            reserved = meter_w + 2 if meter_w else 0
            x = self.put(y + 1, x0 + 2, tx.truncate(detail, width - 2 - reserved), "secondary")
            if nxt and x + 4 + tx.width(nxt) < x1 - reserved:
                self.put(y + 1, x + 4, nxt, "muted")
            if meter_w:
                draw_meter(self.put_attr, self.pal.rgb, self.app.theme.colors, self.meter.bars,
                           y + 1, x1 - meter_w, METER_CELLS, ascii_only=self.sym is ASCII_SYMBOLS)
        else:
            detail = f"  {track.artist}"
            used = 2 + tx.width(track.title)
            if used + 4 < title_w:
                self.put(y, x0 + used, tx.truncate(detail, title_w - used), "secondary")
        self._draw_progress(y + height - 1, x0, x1, player.position, player.duration)

    def _mode_labels(self) -> list[str]:
        player = self.app.player
        labels = {"all": ["repeat all"], "one": ["repeat one"]}.get(player.repeat, [])
        return labels + (["shuffle"] if player.shuffle else [])

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
            if sections == [str(n) for n in range(1, len(SECTIONS) + 1)]:
                section_keys = f"1-{len(SECTIONS)}"
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
