"""Visualizer and artwork rendering.

All values shown are measured from the decoded audio (see playback.mpv.analysis_filter);
this module only adds display dynamics: bars rise instantly and fall smoothly,
and peak markers hold briefly before falling, like the classic Winamp analyzer.
"""

from __future__ import annotations

import math
from typing import Callable

from ..theme import Color

FLOOR_DB, CEIL_DB = -62.0, -14.0   # band levels mapped to 0..1
METER_FLOOR_DB = -50.0
BAR_FALL = 1.8       # full heights per second
PEAK_HOLD = 0.45     # seconds
PEAK_FALL = 0.9

EIGHTHS = " ▁▂▃▄▅▆▇█"
HORIZONTAL = " ▏▎▍▌▋▊▉█"


def normalize(db: float, floor: float = FLOOR_DB, ceil: float = CEIL_DB) -> float:
    if math.isnan(db) or db <= floor:
        return 0.0
    return min(1.0, (db - floor) / (ceil - floor))


class Dynamics:
    """Smoothed bar heights and peak markers for any number of bars."""

    def __init__(self) -> None:
        self.bars: list[float] = []
        self.peaks: list[float] = []
        self._held: list[float] = []

    def update(self, targets: list[float] | None, dt: float) -> bool:
        """Advance by dt seconds towards `targets` (0..1, or None = silence).
        Returns True while anything is still visible."""
        n = len(targets) if targets is not None else len(self.bars)
        if len(self.bars) != n:
            self.bars, self.peaks, self._held = [0.0] * n, [0.0] * n, [0.0] * n
        dt = max(0.0, min(dt, 0.5))
        for i in range(n):
            target = targets[i] if targets is not None else 0.0
            self.bars[i] = target if target >= self.bars[i] else max(target, self.bars[i] - BAR_FALL * dt)
            if self.bars[i] >= self.peaks[i]:
                self.peaks[i], self._held[i] = self.bars[i], 0.0
            else:
                self._held[i] += dt
                if self._held[i] > PEAK_HOLD:
                    self.peaks[i] = max(self.bars[i], self.peaks[i] - PEAK_FALL * dt)
        return any(v > 0.001 for v in self.bars + self.peaks)

    def reset(self) -> None:
        self.bars, self.peaks, self._held = [], [], []


def gradient(low: Color, mid: Color, high: Color, t: float) -> Color:
    """Color at position t (0 bottom .. 1 top) of a three-stop gradient."""
    if low is None or mid is None or high is None:
        return mid
    a, b, f = (low, mid, t * 2) if t < 0.5 else (mid, high, (t - 0.5) * 2)
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))  # type: ignore[return-value]


def bar_layout(width: int, count: int) -> tuple[int, int]:
    """(bar width, gap) that fits `count` bars into `width` cells."""
    for gap in (1, 0):
        bar = (width + gap) // count - gap
        if bar >= 1:
            return min(bar, 4), gap
    return 0, 0


Put = Callable[[int, int, str, int], None]  # (y, x, text, attr)


def draw_spectrum(put: Put, attr_for: Callable[[Color], int], colors: dict, dyn: Dynamics,
                  y: int, x: int, width: int, height: int, style: str = "bars",
                  peaks: bool = True, ascii_only: bool = False) -> None:
    """Draw bars bottom-up in the box (y..y+height-1, x..x+width-1)."""
    count = len(dyn.bars)
    if not count or height <= 0:
        return
    bar_w, gap = bar_layout(width, count)
    if bar_w == 0:
        return
    used = count * bar_w + (count - 1) * gap
    x0 = x + (width - used) // 2
    row_attr = [attr_for(gradient(colors["spectrum_low"], colors["spectrum_mid"],
                                  colors["spectrum_high"], (r + 0.5) / height)) for r in range(height)]
    peak_attr = attr_for(colors["spectrum_peak"])
    for i in range(count):
        cx = x0 + i * (bar_w + gap)
        level = dyn.bars[i] * height
        peak_row = min(height - 1, int(dyn.peaks[i] * height)) if peaks and dyn.peaks[i] > 0.02 else -1
        for r in range(height):  # r = 0 is the bottom row
            fill = level - r
            if style == "segments":
                ch = ("#" if ascii_only else "▆") if fill >= 0.5 else " "
            elif fill >= 1:
                ch = "#" if ascii_only else "█"
            elif fill > 0:
                ch = ("#" if fill >= 0.5 else " ") if ascii_only else EIGHTHS[max(1, int(fill * 8))]
            else:
                ch = " "
            if ch == " " and r == peak_row and r >= level:
                put(y + height - 1 - r, cx, ("-" if ascii_only else "▔") * bar_w, peak_attr)
            elif ch != " ":
                put(y + height - 1 - r, cx, ch * bar_w, row_attr[r])


def draw_meter(put: Put, attr_for: Callable[[Color], int], colors: dict, levels: list[float],
               y: int, x: int, cells: int, ascii_only: bool = False) -> None:
    """Horizontal stereo level meter: two bars separated by a space."""
    steps = " ........#" if ascii_only else HORIZONTAL
    for level in levels[:2]:
        filled = level * cells
        full = int(filled)
        bar = steps[-1] * full
        if full < cells:
            bar += steps[int((filled - full) * (len(steps) - 1))]
        bar = bar[:cells].rstrip()
        put(y, x, bar, attr_for(colors["spectrum_mid"]))
        x += cells + 1


def block_rows(pixels: list[list[tuple[int, int, int]]]) -> list[list[tuple[Color, Color]]]:
    """Pair pixel rows into half-block cells: (top color, bottom color) for '▀'."""
    return [[(top, bottom) for top, bottom in zip(pixels[r], pixels[r + 1])]
            for r in range(0, len(pixels) - 1, 2)]
