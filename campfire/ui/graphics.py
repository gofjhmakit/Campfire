"""Real images through the kitty graphics protocol (kitty, Ghostty), also inside tmux.

Spec: https://sw.kovidgoyal.net/kitty/graphics-protocol/

Campfire uses *Unicode placeholders*. Each image is uploaded once and given a
"virtual placement" of cols x rows cells. The cells where it should appear are
then filled with the placeholder character U+10EEEE plus combining diacritics
for the row and column, in a foreground color that encodes the image id. To
curses and tmux these are ordinary one-cell characters, so images move, get
covered and get redrawn exactly like text. Inside tmux, the upload commands go
through tmux's passthrough (`set -g allow-passthrough on`).

All commands use q=2 so the terminal never answers (answers would arrive as
keyboard input).
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from collections import OrderedDict

CHUNK = 4096
PLACEHOLDER = "\U0010EEEE"
# Row/column diacritics, from kitty's gen/rowcolumn-diacritics.txt (first 64).
DIACRITICS = "\u0305\u030d\u030e\u0310\u0312\u033d\u033e\u033f\u0346\u034a\u034b\u034c\u0350\u0351\u0352\u0357\u035b\u0363\u0364\u0365\u0366\u0367\u0368\u0369\u036a\u036b\u036c\u036d\u036e\u036f\u0483\u0484\u0485\u0486\u0487\u0592\u0593\u0594\u0595\u0597\u0598\u0599\u059c\u059d\u059e\u059f\u05a0\u05a1\u05a8\u05a9\u05ab\u05ac\u05af\u05c4\u0610\u0611\u0612\u0613\u0614\u0615\u0616\u0617\u0657\u0658"
MAX_CELLS = len(DIACRITICS)
# Image ids are sent as a 256-color foreground index. 16-255 only: tmux re-encodes
# 0-15 as basic SGR colors (31, 91, ...) instead of 38;5;N.
FIRST_ID, LAST_ID = 16, 255
GRAPHICS_TERMS = ("xterm-kitty", "xterm-ghostty")


def _tmux(fmt: str) -> str:
    # Ask about the pane Campfire runs in (and the client showing it).
    target = ["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else []
    try:
        return subprocess.run(["tmux", "display-message", *target, "-p", fmt], capture_output=True,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect() -> tuple[str | None, str]:
    """("direct" | "tmux" | None, hint). The hint explains how to enable images."""
    env = os.environ
    if env.get("TMUX"):
        if _tmux("#{client_termname}") not in GRAPHICS_TERMS:
            return None, ""
        if _tmux("#{allow-passthrough}") in ("on", "all"):
            return "tmux", ""
        return None, ("For sharp album art inside tmux, add `set -g allow-passthrough on` "
                      "to ~/.tmux.conf and run `tmux source ~/.tmux.conf`.")
    if env.get("STY") or env.get("TERM", "").startswith("screen"):
        return None, ""
    if (env.get("KITTY_WINDOW_ID") or env.get("TERM") in GRAPHICS_TERMS
            or env.get("TERM_PROGRAM", "").lower() == "ghostty"):
        return "direct", ""
    return None, ""


class KittyImages:
    def __init__(self, tmux: bool = False, write=None):
        self.tmux = tmux
        self._write = write or (lambda data: os.write(sys.stdout.fileno(), data))
        self._ids: OrderedDict[tuple[str, int], int] = OrderedDict()  # (cover, slot) -> id
        self._sizes: dict[int, tuple[int, int]] = {}                  # id -> placed (cols, rows)
        self._lines: dict[tuple[int, int], list[str]] = {}

    def _send(self, command: str) -> None:
        if self.tmux:  # tmux passthrough: wrap and double every ESC
            command = "\x1bPtmux;" + command.replace("\x1b", "\x1b\x1b") + "\x1b\\"
        try:
            self._write(command.encode("ascii"))
        except OSError:
            pass

    def _new_id(self) -> int:
        used = set(self._ids.values())
        for candidate in range(FIRST_ID, LAST_ID + 1):
            if candidate not in used:
                return candidate
        (_, oldest) = self._ids.popitem(last=False)  # evict the least recently used
        self._send(f"\x1b_Ga=d,d=I,i={oldest},q=2\x1b\\")
        self._sizes.pop(oldest, None)
        return oldest

    def image(self, cover: str, png: bytes, slot: int, cols: int, rows: int) -> int:
        """Upload and place as needed; returns the image id to draw placeholders with.
        Each slot gets its own image id, because placeholders can only name an image."""
        key = (cover, slot)
        image_id = self._ids.get(key)
        if image_id is None:
            image_id = self._new_id()
            payload = base64.standard_b64encode(png).decode("ascii")
            chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)] or [""]
            for n, chunk in enumerate(chunks):
                more = 1 if n < len(chunks) - 1 else 0
                head = f"a=t,f=100,t=d,i={image_id},q=2,m={more}" if n == 0 else f"q=2,m={more}"
                self._send(f"\x1b_G{head};{chunk}\x1b\\")
            self._ids[key] = image_id
        self._ids.move_to_end(key)
        if self._sizes.get(image_id) != (cols, rows):
            self._send(f"\x1b_Ga=d,d=i,i={image_id},q=2\x1b\\")  # drop the old virtual placement
            self._send(f"\x1b_Ga=p,U=1,i={image_id},c={cols},r={rows},q=2\x1b\\")
            self._sizes[image_id] = (cols, rows)
        return image_id

    def placeholder_lines(self, cols: int, rows: int) -> list[str]:
        """Text for each row; drawn with the image id as the 256-color foreground."""
        key = (cols, rows)
        if key not in self._lines:
            self._lines[key] = ["".join(PLACEHOLDER + DIACRITICS[r] + DIACRITICS[c] for c in range(cols))
                                for r in range(rows)]
        return self._lines[key]

    def close(self) -> None:
        for image_id in self._ids.values():
            self._send(f"\x1b_Ga=d,d=I,i={image_id},q=2\x1b\\")  # free image data too
        self._ids.clear()
        self._sizes.clear()
