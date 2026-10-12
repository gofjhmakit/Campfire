"""Display-width aware text helpers (wide CJK characters take two cells)."""

from __future__ import annotations

import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(char_width(c) for c in text)


def clean(text: str) -> str:
    """Remove control characters that would corrupt terminal output."""
    return "".join(c if c.isprintable() else " " for c in text)


def truncate(text: str, cells: int, ellipsis: str = "…") -> str:
    """Cut text to at most `cells` display cells, adding an ellipsis if cut."""
    if cells <= 0:
        return ""
    if width(text) <= cells:
        return text
    limit = cells - width(ellipsis)
    out, used = [], 0
    for c in text:
        w = char_width(c)
        if used + w > limit:
            break
        out.append(c)
        used += w
    return "".join(out) + ellipsis


def fit(text: str, cells: int, align: str = "left") -> str:
    """Truncate or pad text to exactly `cells` display cells."""
    text = truncate(text, cells)
    pad = " " * max(0, cells - width(text))
    return pad + text if align == "right" else text + pad


def column_widths(total: int, specs: list[tuple[int, int]], gap: int = 2) -> list[int]:
    """Distribute `total` cells over columns.

    Each spec is (min_width, weight). weight 0 means fixed at min_width.
    Columns are dropped from the right (width 0) when space runs out, but the
    first column is always kept.
    """
    widths = [0] * len(specs)
    active = list(range(len(specs)))
    while active:
        need = sum(specs[i][0] for i in active) + gap * (len(active) - 1)
        if need <= total or len(active) == 1:
            break
        active.pop()
    available = total - gap * (len(active) - 1)
    for i in active:
        widths[i] = specs[i][0]
    spare = available - sum(widths)
    weights = sum(specs[i][1] for i in active)
    if weights and spare > 0:
        flex = [i for i in active if specs[i][1]]
        for i in flex:
            widths[i] += spare * specs[i][1] // weights
        widths[flex[0]] += available - sum(widths)
    if len(active) == 1:
        widths[active[0]] = max(0, total)
    return widths
