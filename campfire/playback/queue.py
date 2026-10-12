"""The playback queue: an ordered list of tracks and a cursor."""

from __future__ import annotations

from ..models import Track


class PlayQueue:
    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self.index: int = -1  # -1: nothing selected

    def __len__(self) -> int:
        return len(self.tracks)

    def set(self, tracks: list[Track], start: int = 0) -> Track | None:
        self.tracks = list(tracks)
        self.index = start if 0 <= start < len(self.tracks) else (-1 if not self.tracks else 0)
        return self.current

    def clear(self) -> None:
        self.tracks, self.index = [], -1

    @property
    def current(self) -> Track | None:
        return self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None

    def has_next(self) -> bool:
        return self.index + 1 < len(self.tracks)

    def has_previous(self) -> bool:
        return self.index > 0

    def next(self) -> Track | None:
        """Advance; returns the new current track or None at the end (cursor unchanged)."""
        if not self.has_next():
            return None
        self.index += 1
        return self.current

    def previous(self) -> Track | None:
        if not self.has_previous():
            return None
        self.index -= 1
        return self.current

    def jump(self, index: int) -> Track | None:
        if 0 <= index < len(self.tracks):
            self.index = index
            return self.current
        return None

    def upcoming(self, limit: int | None = None) -> list[Track]:
        rest = self.tracks[self.index + 1:] if self.index >= 0 else list(self.tracks)
        return rest if limit is None else rest[:limit]
