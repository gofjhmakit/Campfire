"""Shared test helpers. Temporary files live in tests/.tmp (not the system temp dir)."""

import tempfile
from pathlib import Path

from campfire.models import Album, Track

TMP_ROOT = Path(__file__).parent / ".tmp"


def temp_dir() -> tempfile.TemporaryDirectory:
    TMP_ROOT.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=TMP_ROOT)


def album(i: int, artist: str = "Artist", year: int | None = 2020, **kw) -> Album:
    return Album(id=f"al{i}", name=kw.pop("name", f"Album {i}"), artist=artist, year=year, **kw)


def tracks(album: Album, n: int = 3) -> list[Track]:
    return [Track(id=f"{album.id}-t{k}", title=f"Song {k}", artist=album.artist, album=album.name,
                  album_id=album.id, track_no=k, duration=180 + k) for k in range(1, n + 1)]
