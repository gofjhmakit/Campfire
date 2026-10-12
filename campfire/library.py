"""In-memory library index, local search and collection syncing."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .models import Album, Artist, Track, artist_key
from .subsonic import SubsonicClient, SubsonicError

PAGE_SIZE = 500
MAX_PAGES = 200  # 100k albums; guards against a server that ignores offset
ALBUM_WORKERS = 4


class Library:
    def __init__(self, albums: Iterable[Album] = (), tracks: dict[str, list[Track]] | None = None,
                 synced_at: float | None = None):
        self.synced_at = synced_at
        self._albums: dict[str, Album] = {}
        self._tracks: dict[str, list[Track]] = {}
        self._index_dirty = True
        self._artists: dict[str, Artist] | None = None
        for album in albums:
            self._albums[album.id] = album
        for album_id, album_tracks in (tracks or {}).items():
            if album_id in self._albums:
                self._tracks[album_id] = list(album_tracks)

    # -- Mutation ---------------------------------------------------------------

    def set_tracks(self, album: Album, tracks: list[Track]) -> None:
        if album.year is None:
            # Bandcamp's album objects carry no year, but its tracks do.
            years = [t.year for t in tracks if t.year]
            if years:
                album = replace(album, year=max(set(years), key=years.count))
        self._albums[album.id] = album
        self._tracks[album.id] = tracks
        self._index_dirty = True
        self._artists = None

    def __len__(self) -> int:
        return len(self._albums)

    # -- Queries ----------------------------------------------------------------

    def album(self, album_id: str) -> Album | None:
        return self._albums.get(album_id)

    def albums(self) -> list[Album]:
        return sorted(self._albums.values(), key=lambda a: (a.name.casefold(), a.artist.casefold()))

    def tracks_of(self, album_id: str) -> list[Track] | None:
        """Tracks of an album, or None if they have not been loaded yet."""
        return self._tracks.get(album_id)

    def has_tracks(self, album_id: str) -> bool:
        return album_id in self._tracks

    def missing_track_albums(self) -> list[Album]:
        return [a for a in self.albums() if a.id not in self._tracks]

    def all_tracks(self) -> list[Track]:
        tracks = [t for a in self.albums() for t in self._tracks.get(a.id, [])]
        return sorted(tracks, key=lambda t: (t.title.casefold(), t.artist.casefold()))

    def artists(self) -> list[Artist]:
        return list(self._artist_map().values())

    def _artist_map(self) -> dict[str, Artist]:
        if self._artists is None:
            by_key: dict[str, Artist] = {}
            for album in sorted(self._albums.values(), key=lambda a: (a.year or 0, a.name.casefold())):
                key = artist_key(album.artist)
                artist = by_key.setdefault(key, Artist(key=key, name=album.artist))
                artist.album_ids.append(album.id)
            self._artists = dict(sorted(by_key.items()))
        return self._artists

    def artist(self, key: str) -> Artist | None:
        return self._artist_map().get(key)

    def artist_albums(self, key: str) -> list[Album]:
        artist = self.artist(key)
        return [self._albums[i] for i in artist.album_ids] if artist else []

    def artist_tracks(self, key: str) -> list[Track]:
        """All loaded tracks of an artist's albums, album by album in release order."""
        return [t for a in self.artist_albums(key) for t in self._tracks.get(a.id, [])]

    # -- Search -----------------------------------------------------------------

    def search(self, query: str, limit: int = 200) -> list["SearchResult"]:
        terms = query.casefold().split()
        if not terms:
            return []
        self._build_index()
        results: list[SearchResult] = []
        for kind, item, haystack, primary in self._index:
            if all(term in haystack for term in terms):
                results.append(SearchResult(kind, item, _score(terms, primary, kind)))
        results.sort(key=lambda r: (-r.score, _KIND_ORDER[r.kind], r.label.casefold()))
        return results[:limit]

    def _build_index(self) -> None:
        if not self._index_dirty:
            return
        index: list[tuple[str, object, str, str]] = []
        for artist in self.artists():
            index.append(("artist", artist, artist.key, artist.key))
        for album in self._albums.values():
            index.append(("album", album, f"{album.name} {album.artist}".casefold(),
                          album.name.casefold()))
        for tracks in self._tracks.values():
            for t in tracks:
                index.append(("track", t, f"{t.title} {t.artist} {t.album}".casefold(),
                              t.title.casefold()))
        self._index = index
        self._index_dirty = False


_KIND_ORDER = {"artist": 0, "album": 1, "track": 2}


@dataclass
class SearchResult:
    kind: str  # "artist" | "album" | "track"
    item: Artist | Album | Track
    score: int

    @property
    def label(self) -> str:
        return getattr(self.item, "name", None) or getattr(self.item, "title", "")


def _score(terms: list[str], primary: str, kind: str) -> int:
    """Rank exact > prefix > word-start > substring matches of the item's own name."""
    phrase = " ".join(terms)
    if primary == phrase:
        score = 100
    elif primary.startswith(phrase):
        score = 80
    elif f" {phrase}" in f" {primary}":
        score = 60
    elif phrase in primary:
        score = 40
    else:
        score = 10  # matched only through other fields (artist / album)
    return score + {"artist": 3, "album": 2, "track": 0}[kind]


# -- Syncing ---------------------------------------------------------------------

Progress = Callable[[str, int, int], None]  # (phase, done, total)


def fetch_albums(client: SubsonicClient, progress: Progress | None = None) -> list[Album]:
    """Fetch the complete album list, page by page."""
    albums: dict[str, Album] = {}
    offset = 0
    previous_first = None
    for _ in range(MAX_PAGES):
        page = client.album_list_page(offset, PAGE_SIZE)
        if not page:
            break
        if page[0].id == previous_first:  # server ignoring offset: stop instead of looping
            break
        previous_first = page[0].id
        for album in page:
            albums.setdefault(album.id, album)
        offset += len(page)  # the server may cap the page size below PAGE_SIZE
        if progress:
            progress("albums", len(albums), 0)
    return list(albums.values())


def fetch_tracks(client: SubsonicClient, albums: list[Album],
                 on_album: Callable[[Album, list[Track]], None],
                 progress: Progress | None = None,
                 should_stop: Callable[[], bool] = lambda: False) -> list[tuple[Album, str]]:
    """Fetch track listings for albums concurrently. Returns failures (album, message)."""
    failures: list[tuple[Album, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=ALBUM_WORKERS) as pool:
        futures = {pool.submit(client.album, a.id): a for a in albums}
        for future in as_completed(futures):
            album = futures[future]
            done += 1
            if should_stop():
                for f in futures:
                    f.cancel()
                break
            try:
                full_album, tracks = future.result()
                on_album(full_album, tracks)
            except SubsonicError as exc:
                failures.append((album, str(exc)))
            if progress:
                progress("tracks", done, len(albums))
    return failures


def is_stale(library: Library, max_age_hours: float) -> bool:
    if library.synced_at is None or not len(library):
        return True
    return time.time() - library.synced_at > max_age_hours * 3600
