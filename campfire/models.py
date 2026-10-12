"""Library data models and parsing of Subsonic (OpenSubsonic) JSON objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
    album_id: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    duration: int | None = None  # seconds
    year: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    artist: str
    artist_id: str = ""
    year: int | None = None
    song_count: int | None = None
    duration: int | None = None
    genre: str = ""
    created: str = ""  # ISO timestamp; for Bandcamp likely the purchase/add date

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Album":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Artist:
    """An artist as represented in the collection (derived from albums)."""
    key: str
    name: str
    album_ids: list[str] = field(default_factory=list)


def artist_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def parse_album(d: dict) -> Album:
    """Parse an AlbumID3 object (getAlbumList2 / getAlbum / search3)."""
    if not d.get("id"):
        raise ValueError("album without id")
    return Album(
        id=_str(d["id"]),
        name=_str(d.get("name") or d.get("title")) or "Unknown album",
        artist=_str(d.get("artist") or d.get("displayArtist")) or "Unknown artist",
        artist_id=_str(d.get("artistId")),
        year=_int(d.get("year")),
        song_count=_int(d.get("songCount")),
        duration=_int(d.get("duration")),
        genre=_str(d.get("genre")),
        created=_str(d.get("created")),
    )


def parse_track(d: dict, album: Album | None = None) -> Track:
    """Parse a Child (song) object."""
    if not d.get("id"):
        raise ValueError("track without id")
    return Track(
        id=_str(d["id"]),
        title=_str(d.get("title")) or "Untitled",
        artist=_str(d.get("artist") or d.get("displayArtist")) or (album.artist if album else "Unknown artist"),
        album=_str(d.get("album")) or (album.name if album else ""),
        album_id=_str(d.get("albumId") or d.get("parent")) or (album.id if album else ""),
        track_no=_int(d.get("track")),
        disc_no=_int(d.get("discNumber")),
        duration=_int(d.get("duration")),
        year=_int(d.get("year")) or (album.year if album else None),
    )


def parse_items(items: object, parser, *args) -> list:
    """Parse a list of objects, skipping malformed entries instead of failing."""
    if isinstance(items, dict):  # XML-to-JSON conversions sometimes collapse 1-item lists
        items = [items]
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            try:
                out.append(parser(item, *args))
            except ValueError:
                continue
    return out


def sort_album_tracks(tracks: list[Track]) -> list[Track]:
    # Stable: keeps server order for tracks without numbers.
    return sorted(tracks, key=lambda t: (t.disc_no or 1, t.track_no if t.track_no is not None else 10**6))


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
