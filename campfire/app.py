"""Application core: wires the library, sync, cache and playback together.

Everything here except the worker threads runs on the UI thread. Workers never
touch shared state; they post events to the EventBus, which the UI loop drains.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass

from . import art
from .cache import Cache
from .config import Settings, cache_dir, runtime_dir
from .credentials import Credentials
from .library import Library, fetch_albums, fetch_tracks, is_stale
from .models import Album, Track
from .playback.controller import PlaybackController
from .playback.mpv import MpvBackend
from .subsonic import AuthError, EmptyServerError, NetworkError, SubsonicClient, SubsonicError
from .theme import Theme

NOTICE_SECONDS = {"info": 4.0, "warning": 8.0, "error": 12.0}


def _make_private(path) -> None:
    """Restrict an existing cache directory created with default permissions."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        for item in path.glob("library.sqlite3*"):
            os.chmod(item, 0o600)
    except OSError:
        pass


class EventBus:
    """Thread-safe queue whose `fileno()` becomes readable when events arrive,
    so the UI can sleep in select() instead of polling."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        os.set_blocking(self._w, False)

    def put(self, event: tuple) -> None:
        self._queue.put(event)
        self.wake()

    def wake(self) -> None:
        try:
            os.write(self._w, b"x")
        except (BlockingIOError, OSError):
            pass  # pipe full: a wakeup is already pending

    def fileno(self) -> int:
        return self._r

    def drain(self) -> list[tuple]:
        try:
            while os.read(self._r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass
class Notice:
    level: str
    text: str
    expires: float


@dataclass
class SyncState:
    running: bool = False
    phase: str = ""
    done: int = 0
    total: int = 0


class App:
    def __init__(self, settings: Settings, theme: Theme, credentials: Credentials,
                 startup_warnings: list[str] | None = None):
        self.settings = settings
        self.theme = theme
        self.bus = EventBus()
        self.client = SubsonicClient(settings.server_url, credentials.username, credentials.password)
        self.cache = Cache(cache_dir() / "library.sqlite3", credentials.username, settings.server_url)
        _make_private(cache_dir())
        self.library = self.cache.load() or Library()
        self.library_version = 0  # bumped whenever library contents change
        self.sync = SyncState()
        self.sync_error: str | None = None  # last failed refresh, for empty views
        self._stop_sync = threading.Event()
        self._album_requests: set[str] = set()
        self.notices: list[Notice] = []
        self.backend = MpvBackend(self.bus, runtime_dir(), settings.mpv_path,
                                  settings.volume, visualizer=settings.visualizer != "off",
                                  user_config=settings.mpv_user_config,
                                  bands=settings.spectrum_bands)
        self.art_cache = art.ArtCache(cache_dir() / "art")
        self.artwork: dict[str, art.Artwork | None] = {}  # None: unavailable
        self._art_requests: set[str] = set()
        self._art_retry_after: dict[str, float] = {}
        self.art_enabled = settings.album_art != "off" and art.ffmpeg_path() is not None
        self.player = PlaybackController(
            self.backend, lambda track: self.client.stream_url(track.id), self.notify,
            seek_seconds=settings.seek_seconds,
            previous_restart_seconds=settings.previous_restart_seconds,
            volume=settings.volume, repeat=settings.repeat, shuffle=settings.shuffle)
        for warning in startup_warnings or []:
            self.notify("warning", warning)

    @property
    def mpv_available(self) -> bool:
        return MpvBackend.find(self.settings.mpv_path) is not None

    def start(self) -> None:
        if self.settings.album_art != "off" and not self.art_enabled:
            self.notify("info", "Album art needs the ffmpeg command-line tool, which was not found.")
        if not self.mpv_available:
            self.notify("error", f"mpv not found ('{self.settings.mpv_path}'): playback is unavailable. "
                                 "Install it with `brew install mpv` or `sudo apt install mpv`.")
        if is_stale(self.library, self.settings.cache_max_age_hours):
            self.refresh()

    def shutdown(self) -> None:
        self._stop_sync.set()
        self.player.shutdown()
        self.bus.close()

    # -- Notifications -------------------------------------------------------------

    def notify(self, level: str, text: str) -> None:
        # Drop an identical message that is already showing.
        if self.notices and self.notices[-1].text == text:
            self.notices[-1].expires = time.monotonic() + NOTICE_SECONDS.get(level, 5)
            return
        self.notices.append(Notice(level, text, time.monotonic() + NOTICE_SECONDS.get(level, 5)))
        del self.notices[:-20]

    def current_notice(self) -> Notice | None:
        """The newest notice that has not expired."""
        now = time.monotonic()
        self.notices = [n for n in self.notices if n.expires >= now]
        return self.notices[-1] if self.notices else None

    def active_notice_count(self) -> int:
        now = time.monotonic()
        return sum(1 for n in self.notices if n.expires >= now)

    def next_notice_expiry(self) -> float | None:
        return min((n.expires for n in self.notices), default=None)

    # -- Library syncing ----------------------------------------------------------------

    def refresh(self) -> None:
        if self.sync.running:
            self.notify("info", "Library refresh already in progress.")
            return
        self.sync = SyncState(running=True, phase="albums")
        self.sync_error = None
        self._stop_sync.clear()
        threading.Thread(target=self._sync_worker, name="library-sync", daemon=True).start()

    def _sync_worker(self) -> None:
        post = self.bus.put
        try:
            albums = fetch_albums(self.client, lambda phase, done, total: post(("sync-progress", phase, done, total)))
        except SubsonicError as exc:
            post(("sync-error", exc))
            return
        post(("sync-albums", albums))
        failures = fetch_tracks(
            self.client, albums,
            on_album=lambda album, tracks: post(("album-tracks", album, tracks)),
            progress=lambda phase, done, total: post(("sync-progress", phase, done, total)),
            should_stop=self._stop_sync.is_set)
        post(("sync-done", failures))

    def request_album(self, album_id: str) -> None:
        """Fetch one album's tracks now (e.g. the user opened it before sync reached it)."""
        if album_id in self._album_requests or self.library.has_tracks(album_id):
            return
        self._album_requests.add(album_id)

        def worker() -> None:
            try:
                album, tracks = self.client.album(album_id)
                self.bus.put(("album-tracks", album, tracks))
            except SubsonicError as exc:
                self.bus.put(("album-error", album_id, exc))
        threading.Thread(target=worker, name="album-fetch", daemon=True).start()

    def album_artwork(self, album: Album | None) -> art.Artwork | None:
        """Decoded artwork if available; otherwise starts loading it in the background."""
        if not self.art_enabled or album is None or not album.cover_art:
            return None
        cover = album.cover_art
        if cover in self.artwork:
            return self.artwork[cover]
        if cover not in self._art_requests and time.monotonic() >= self._art_retry_after.get(cover, 0):
            self._art_requests.add(cover)
            threading.Thread(target=self._art_worker, args=(cover,), name="art", daemon=True).start()
        return None

    def _art_worker(self, cover: str) -> None:
        data = self.art_cache.get(cover)
        try:
            if data is None:
                data = self.client.cover_art(cover)
                self.art_cache.put(cover, data)
            self.bus.put(("art", cover, art.decode(cover, data)))
        except art.ArtError:
            self.art_cache.discard(cover)  # never keep a file that does not decode
            self.bus.put(("art", cover, None))
        except SubsonicError:
            self.bus.put(("art-retry", cover))

    # -- Event processing ---------------------------------------------------------------

    def process_events(self) -> bool:
        events = self.bus.drain()
        for event in events:
            kind = event[0]
            if kind == "mpv":
                self.player.handle_event(event[1])
            elif kind == "sync-progress":
                _, self.sync.phase, self.sync.done, self.sync.total = event
            elif kind == "sync-albums":
                self._apply_album_list(event[1])
            elif kind == "album-tracks":
                _, album, tracks = event
                self._album_requests.discard(album.id)
                self.library.set_tracks(album, tracks)
                self.library_version += 1
            elif kind == "album-error":
                _, album_id, exc = event
                self._album_requests.discard(album_id)
                self.notify("error", f"Could not load album: {exc}")
            elif kind == "art":
                _, cover, artwork = event
                self._art_requests.discard(cover)
                self.artwork[cover] = artwork
                while len(self.artwork) > 64:  # bounded memory
                    del self.artwork[next(iter(self.artwork))]
            elif kind == "art-retry":
                self._art_requests.discard(event[1])  # network trouble: try again later
                self._art_retry_after[event[1]] = time.monotonic() + 30
            elif kind == "sync-done":
                self._finish_sync(event[1])
            elif kind == "sync-error":
                self._sync_failed(event[1])
        return bool(events)

    def _apply_album_list(self, albums: list[Album]) -> None:
        # Keep known track listings while fresh ones are fetched.
        old = self.library
        tracks = {a.id: old.tracks_of(a.id) for a in albums if old.has_tracks(a.id)}
        self.library = Library(albums, tracks, synced_at=None)
        self.library_version += 1

    def _finish_sync(self, failures: list[tuple[Album, str]]) -> None:
        self.sync = SyncState()
        if self._stop_sync.is_set():
            return
        self.library.synced_at = time.time()
        try:
            self.cache.save(self.library, complete=not failures)
        except Exception as exc:  # the cache is an optimisation; never fatal
            self.notify("warning", f"Could not write the library cache: {exc}")
        if failures:
            self.notify("warning", f"{len(failures)} album(s) could not be loaded: {failures[0][1]}")
        else:
            self.notify("info", f"Library updated: {len(self.library)} albums.")

    def _sync_failed(self, exc: SubsonicError) -> None:
        self.sync = SyncState()
        self.sync_error = str(exc)
        cached = " Showing the cached library." if len(self.library) else ""
        if isinstance(exc, (AuthError, EmptyServerError)):
            self.notify("error", "Bandcamp rejected the Subsonic credentials. "
                                 f"Run `campfire login` to update them.{cached}")
        elif isinstance(exc, NetworkError):
            self.notify("error", f"{exc}{cached}")
        else:
            self.notify("error", f"Library refresh failed: {exc}{cached}")

    # -- Playback actions -------------------------------------------------------------------

    def play_album(self, album_id: str, start_track_id: str | None = None,
                   shuffle: bool | None = None) -> None:
        tracks = self.library.tracks_of(album_id)
        if tracks is None:
            self.request_album(album_id)
            self.notify("info", "Loading album tracks… try again in a moment.")
            return
        start = next((i for i, t in enumerate(tracks) if t.id == start_track_id), 0)
        self.player.play(tracks, start, shuffle=shuffle, chosen=start_track_id is not None)

    def play_all(self, shuffle: bool | None = True) -> None:
        """Every loaded track in the collection."""
        tracks = self.library.all_tracks()
        if self.sync.running:
            self.notify("info", "The library is still loading; using the tracks loaded so far.")
        self.player.play(tracks, shuffle=shuffle)

    def play_artist(self, key: str, shuffle: bool | None = None) -> None:
        artist = self.library.artist(key)
        if not artist:
            return
        missing = [a for a in artist.album_ids if not self.library.has_tracks(a)]
        for album_id in missing:
            self.request_album(album_id)
        tracks = self.library.artist_tracks(key)
        if missing:
            self.notify("info", f"{len(missing)} album(s) still loading; playing what is available.")
        self.player.play(tracks, shuffle=shuffle)

    def play_tracks(self, tracks: list[Track], start: int = 0) -> None:
        self.player.play(tracks, start)
