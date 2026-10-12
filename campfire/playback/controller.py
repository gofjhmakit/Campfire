"""Playback controller: the queue plus the state reported by the audio backend.

All methods are called from the UI thread; backend events reach it through the
application's event queue (`handle_event`), so no locking is needed here.
The displayed state is derived from what mpv reports, not from what we asked for.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Protocol

from ..models import Track
from .mpv import BackendError
from .queue import PlayQueue

MAX_CONSECUTIVE_FAILURES = 3


class Backend(Protocol):
    @property
    def running(self) -> bool: ...
    def start(self) -> None: ...
    def load(self, url: str) -> None: ...
    def set_pause(self, paused: bool) -> None: ...
    def seek(self, seconds: float, absolute: bool = False) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def analysis(self): ...
    def shutdown(self) -> None: ...
    visualizer: bool
    def disable_visualizer(self) -> None: ...


class Status(Enum):
    STOPPED = "stopped"
    LOADING = "loading"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"


Notify = Callable[[str, str], None]  # (level: "info" | "warning" | "error", message)


class PlaybackController:
    def __init__(self, backend: Backend, stream_url: Callable[[Track], str], notify: Notify,
                 seek_seconds: int = 10, previous_restart_seconds: float = 3.0, volume: int = 80):
        self.backend = backend
        self.stream_url = stream_url
        self.notify = notify
        self.seek_seconds = seek_seconds
        self.previous_restart_seconds = previous_restart_seconds
        self.queue = PlayQueue()
        self.volume = volume
        self.position: float | None = None
        self.duration: float | None = None
        self.paused = False
        self.buffering = False
        self.seekable: bool | None = None
        self.idle = True
        self._stopped = True
        self._loading = False
        self._entry_id: int | None = None   # mpv playlist entry of the current file
        self._replaced_id: int | None = None  # entry being replaced by a new load
        self._failures = 0

    # -- State -------------------------------------------------------------------

    @property
    def current(self) -> Track | None:
        return self.queue.current

    @property
    def status(self) -> Status:
        if self._stopped or self.current is None:
            return Status.STOPPED
        if self._loading:
            return Status.LOADING
        if self.paused:
            return Status.PAUSED
        if self.buffering:
            return Status.BUFFERING
        if self.idle:
            return Status.STOPPED
        return Status.PLAYING

    @property
    def active(self) -> bool:
        """True while something is (or is about to be) audible."""
        return self.status in (Status.PLAYING, Status.LOADING, Status.BUFFERING)

    # -- Commands ----------------------------------------------------------------

    def play(self, tracks: list[Track], start: int = 0) -> None:
        if not tracks:
            self.notify("warning", "Nothing to play.")
            return
        self._failures = 0
        self._start(self.queue.set(tracks, start))

    def jump(self, index: int) -> None:
        self._failures = 0
        self._start(self.queue.jump(index))

    def toggle_pause(self) -> None:
        if self.current is None:
            return
        if self.status == Status.STOPPED:
            self._start(self.current)
            return
        self._backend_call(self.backend.set_pause, not self.paused)

    def stop(self) -> None:
        if self.backend.running:
            self._backend_call(self.backend.stop)
        self._stopped = True
        self._loading = False
        self.position = None

    def next(self) -> None:
        track = self.queue.next()
        if track is None:
            self.notify("info", "End of queue.")
            return
        self._failures = 0
        self._start(track)

    def previous(self) -> None:
        if self.current is None:
            return
        position = self.position or 0.0
        if position > self.previous_restart_seconds or not self.queue.has_previous():
            if self.status == Status.STOPPED:
                self._start(self.current)
            else:
                self._backend_call(self.backend.seek, 0, True)
            return
        self._failures = 0
        self._start(self.queue.previous())

    def seek(self, direction: int) -> None:
        if self.status in (Status.STOPPED, Status.LOADING):
            return
        if self.seekable is False:
            self.notify("warning", "This stream does not support seeking.")
            return
        self._backend_call(self.backend.seek, direction * self.seek_seconds)

    def change_volume(self, delta: int) -> None:
        self.volume = max(0, min(100, self.volume + delta))
        if self.backend.running:
            self._backend_call(self.backend.set_volume, self.volume)

    def analysis(self):
        """Spectrum/level measurements while playing (see mpv.Analysis), else None."""
        return self.backend.analysis() if self.status == Status.PLAYING else None

    def shutdown(self) -> None:
        self.backend.shutdown()

    # -- Internals ---------------------------------------------------------------------

    def _start(self, track: Track | None) -> None:
        if track is None:
            return
        try:
            if not self.backend.running:
                self.backend.start()
                self.backend.set_volume(self.volume)
            self.backend.load(self.stream_url(track))
        except BackendError as exc:
            self._stopped = True
            self._loading = False
            self.notify("error", str(exc))
            return
        self._stopped = False
        self._loading = True
        if self._entry_id is not None:
            self._replaced_id = self._entry_id
        self._entry_id = None
        self.position = 0.0
        self.duration = float(track.duration) if track.duration else None
        self.seekable = None

    def _backend_call(self, fn, *args) -> None:
        try:
            fn(*args)
        except BackendError as exc:
            self.notify("error", str(exc))

    def handle_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "property-change":
            self._property(msg.get("name"), msg.get("data"))
        elif event == "start-file":
            self._entry_id = msg.get("playlist_entry_id")
        elif event == "file-loaded":
            self._loading = False
            self._failures = 0
        elif event == "playback-restart":
            self._loading = False
        elif event == "end-file":
            self._end_file(msg)
        elif event == "command-error":
            if msg.get("command") == "seek":
                self.notify("warning", "Seeking is not possible in this stream right now.")
            else:
                self.notify("error", f"mpv could not run '{msg.get('command')}': {msg.get('error')}")
        elif event == "backend-exited":
            was_playing = not self._stopped
            self._stopped = True
            self._loading = False
            self.idle = True
            if was_playing:
                self.notify("error", "mpv exited unexpectedly. Press Space to restart the track.")

    def _property(self, name: str | None, value: object) -> None:
        if name == "time-pos":
            self.position = float(value) if isinstance(value, (int, float)) else self.position
        elif name == "duration":
            if isinstance(value, (int, float)) and value > 0:
                self.duration = float(value)
        elif name == "pause":
            self.paused = bool(value)
        elif name == "paused-for-cache":
            self.buffering = bool(value)
        elif name == "idle-active":
            self.idle = bool(value)
        elif name == "seekable":
            self.seekable = bool(value) if value is not None else None
        elif name == "volume" and isinstance(value, (int, float)):
            self.volume = int(round(value))

    def _end_file(self, msg: dict) -> None:
        entry = msg.get("playlist_entry_id")
        if entry is not None and (entry == self._replaced_id or
                                  (self._entry_id is not None and entry != self._entry_id)):
            return  # a previous file that we replaced
        reason = msg.get("reason")
        if reason == "eof":
            self._failures = 0
            self._advance_or_finish()
        elif reason == "error":
            if self.backend.visualizer and self.current is not None:
                # The analysis filter can make mpv reject a file (e.g. an unusual
                # channel layout). Playback matters more: drop it and retry.
                self.backend.disable_visualizer()
                self.notify("warning", "The visualizer could not process this track and was turned off.")
                self._start(self.current)
                return
            self._failures += 1
            title = self.current.title if self.current else "track"
            detail = msg.get("file_error") or "unknown error"
            self.notify("error", f"Could not play “{title}”: {detail}.")
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._stopped, self._loading = True, False
                self.notify("error", "Stopped after several tracks failed in a row. "
                                     "Check your network connection or credentials.")
            else:
                self._advance_or_finish()

    def _advance_or_finish(self) -> None:
        track = self.queue.next()
        if track is None:
            self._stopped, self._loading = True, False
            self.position = None
        else:
            self._start(track)
