"""Navigable list views. Views build rows from the library; the TUI draws them.

Views never perform I/O themselves: they ask the App (which runs network work
in background threads) and return the next view to show, if any.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import Album, Artist, format_duration

if TYPE_CHECKING:
    from ..app import App


@dataclass
class Row:
    cells: tuple[str, ...]
    kind: str  # "action" | "artist" | "album" | "track"
    item: object = None
    track_id: str | None = None   # rows of a playable track (for the ▶ marker)
    album_id: str | None = None   # rows of an album (marker when it is playing)


Column = tuple[int, int, str]  # (min width, flex weight, align)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _sync_hint(app: "App") -> str:
    s = app.sync
    if not s.running:
        if app.sync_error and not len(app.library):
            return "Could not load your collection. Press r to retry."
        return ""
    if s.phase == "tracks" and s.total:
        return f"Loading tracks… {s.done}/{s.total} albums"
    return "Loading your collection…" + (f" {s.done} albums" if s.done else "")


class View:
    title = ""
    root = ""            # which sidebar entry this view belongs to
    headers: tuple[str, ...] = ()
    columns: list[Column] = []

    def __init__(self) -> None:
        self.cursor = 0
        self.scroll = 0
        self._rows: list[Row] = []
        self._key: object = None
        self.pending_track: str | None = None  # move the cursor here once it appears

    def cache_key(self, app: "App") -> object:
        return app.library_version

    def rows(self, app: "App") -> list[Row]:
        key = self.cache_key(app)
        if key != self._key:
            self._rows = self.build(app)
            self._key = key
            self.cursor = min(self.cursor, max(0, len(self._rows) - 1))
            if self.pending_track:
                for i, row in enumerate(self._rows):
                    if row.track_id == self.pending_track:
                        self.cursor, self.pending_track = i, None
                        break
        return self._rows

    def build(self, app: "App") -> list[Row]:
        return []

    def subtitle(self, app: "App") -> str:
        return ""

    def empty_message(self, app: "App") -> str:
        return _sync_hint(app) or "Nothing here."

    def activate(self, app: "App", row: Row) -> "View | None":
        return None

    def selected(self, app: "App") -> Row | None:
        rows = self.rows(app)
        return rows[self.cursor] if 0 <= self.cursor < len(rows) else None



class ArtistsView(View):
    title = "Artists"
    root = "artists"
    headers = ("ARTIST", "ALBUMS")
    columns = [(12, 1, "left"), (6, 0, "right")]

    def build(self, app):
        return [Row((a.name, str(len(a.album_ids))), "artist", a) for a in app.library.artists()]

    def subtitle(self, app):
        return str(len(self._rows)) if self._rows else ""

    def empty_message(self, app):
        return _sync_hint(app) or "No artists in your collection."

    def activate(self, app, row):
        return ArtistView(row.item.key)


class ArtistView(View):
    root = "artists"
    headers = ("", "YEAR", "TRACKS")
    columns = [(12, 1, "left"), (4, 0, "right"), (6, 0, "right")]

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def build(self, app):
        artist: Artist | None = app.library.artist(self.key)
        if not artist:
            return []
        self.title = artist.name
        albums = app.library.artist_albums(self.key)
        count = sum(len(app.library.tracks_of(a.id) or []) for a in albums)
        rows = [Row(("Play all tracks", "", str(count) if count else ""), "action", "play-artist")]
        for album in albums:
            tracks = app.library.tracks_of(album.id)
            n = len(tracks) if tracks is not None else album.song_count
            rows.append(Row((album.name, str(album.year or ""), str(n) if n else ""),
                            "album", album, album_id=album.id))
        return rows

    def subtitle(self, app):
        return _plural(max(0, len(self._rows) - 1), "album")

    def activate(self, app, row):
        if row.kind == "action":
            app.play_artist(self.key)
            return None
        return AlbumView(row.item.id)


class AlbumsView(View):
    title = "Albums"
    root = "albums"
    headers = ("ALBUM", "ARTIST", "YEAR", "TRACKS")
    columns = [(12, 3, "left"), (10, 2, "left"), (4, 0, "right"), (6, 0, "right")]

    def build(self, app):
        rows = []
        for a in app.library.albums():
            tracks = app.library.tracks_of(a.id)
            n = len(tracks) if tracks is not None else a.song_count
            rows.append(Row((a.name, a.artist, str(a.year or ""), str(n) if n else ""),
                            "album", a, album_id=a.id))
        return rows

    def subtitle(self, app):
        return str(len(self._rows)) if self._rows else ""

    def empty_message(self, app):
        return _sync_hint(app) or "No albums in your collection."

    def activate(self, app, row):
        return AlbumView(row.item.id)


class AlbumView(View):
    root = "albums"
    columns = [(3, 0, "right"), (12, 3, "left"), (10, 2, "left"), (5, 0, "right")]

    def __init__(self, album_id: str):
        super().__init__()
        self.album_id = album_id

    def build(self, app):
        album: Album | None = app.library.album(self.album_id)
        if not album:
            return []
        self.title = album.name
        tracks = app.library.tracks_of(album.id)
        if tracks is None:
            app.request_album(album.id)
            return []
        various = any(t.artist != album.artist for t in tracks)
        self.headers = ("#", "TITLE", "ARTIST" if various else "", "TIME")
        total = sum(t.duration or 0 for t in tracks)
        rows = [Row(("", "Play album", "", format_duration(total) if total else ""), "action", "play-album")]
        for i, t in enumerate(tracks, 1):
            rows.append(Row((str(t.track_no or i), t.title, t.artist if various else "",
                             format_duration(t.duration)), "track", t, track_id=t.id))
        return rows

    def subtitle(self, app):
        album = app.library.album(self.album_id)
        if not album:
            return ""
        parts = [album.artist]
        if album.year:
            parts.append(str(album.year))
        if album.genre:
            parts.append(album.genre)
        if self._rows:
            parts.append(_plural(len(self._rows) - 1, "track"))
        return " · ".join(parts)

    def empty_message(self, app):
        if not app.library.album(self.album_id):
            return "This album is no longer in your collection."
        return "Loading tracks…"

    def activate(self, app, row):
        if row.kind == "action":
            app.play_album(self.album_id)
        else:
            app.play_album(self.album_id, row.track_id)
        return None


class TracksView(View):
    title = "Tracks"
    root = "tracks"
    headers = ("TITLE", "ARTIST", "ALBUM", "TIME")
    columns = [(12, 3, "left"), (10, 2, "left"), (10, 2, "left"), (5, 0, "right")]

    def build(self, app):
        return [Row((t.title, t.artist, t.album, format_duration(t.duration)), "track", t, track_id=t.id)
                for t in app.library.all_tracks()]

    def subtitle(self, app):
        return str(len(self._rows)) if self._rows else ""

    def empty_message(self, app):
        return _sync_hint(app) or "No tracks loaded."

    def activate(self, app, row):
        # Play the track and continue with the rest of its album.
        app.play_album(row.item.album_id, row.track_id)
        return None


class QueueView(View):
    title = "Queue"
    root = "queue"
    headers = ("#", "TITLE", "ARTIST", "TIME")
    columns = [(3, 0, "right"), (12, 3, "left"), (10, 2, "left"), (5, 0, "right")]

    def cache_key(self, app):
        q = app.player.queue
        return (id(q.tracks), len(q.tracks))

    def build(self, app):
        return [Row((str(i), t.title, t.artist, format_duration(t.duration)), "track", i, track_id=t.id)
                for i, t in enumerate(app.player.queue.tracks, 1)]

    def subtitle(self, app):
        q = app.player.queue
        if not len(q):
            return ""
        return f"{_plural(len(q), 'track')} · {len(q.upcoming())} upcoming"

    def empty_message(self, app):
        return "The queue is empty. Play an album, artist or track to fill it."

    def activate(self, app, row):
        app.player.jump(row.item - 1)
        return None


class SearchView(View):
    title = "Search"
    root = "search"
    headers = ("TYPE", "NAME", "DETAILS")
    columns = [(6, 0, "left"), (12, 3, "left"), (10, 2, "left")]

    def __init__(self) -> None:
        super().__init__()
        self.query = ""

    def cache_key(self, app):
        return (app.library_version, self.query)

    def build(self, app):
        rows = []
        for r in app.library.search(self.query):
            item = r.item
            if r.kind == "artist":
                rows.append(Row(("artist", item.name, _plural(len(item.album_ids), "album")), "artist", item))
            elif r.kind == "album":
                detail = item.artist + (f" · {item.year}" if item.year else "")
                rows.append(Row(("album", item.name, detail), "album", item, album_id=item.id))
            else:
                rows.append(Row(("track", item.title, f"{item.artist} — {item.album}"), "track",
                                item, track_id=item.id))
        return rows

    def subtitle(self, app):
        return _plural(len(self._rows), "result") if self.query.strip() else ""

    def empty_message(self, app):
        if not self.query.strip():
            return "Type to search artists, albums and tracks."
        return "No matches." + (f"  ({_sync_hint(app)})" if app.sync.running else "")

    def activate(self, app, row):
        if row.kind == "artist":
            return ArtistView(row.item.key)
        view = AlbumView(row.item.id if row.kind == "album" else row.item.album_id)
        if row.kind == "track":
            view.pending_track = row.track_id
        return view
