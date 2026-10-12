import sqlite3
import time
import unittest
from dataclasses import replace
from pathlib import Path

from campfire import cache as cache_mod
from campfire.cache import Cache
from campfire.library import Library, fetch_albums, fetch_tracks, is_stale
from campfire.models import Album
from campfire.subsonic import NetworkError

from .helpers import album, temp_dir, tracks


def sample_library() -> Library:
    a1 = album(1, "Boards of Canada", 1998, name="Music Has the Right to Children")
    a2 = album(2, "boards of canada ", 2005, name="The Campfire Headphase")
    a3 = album(3, "Grouper", 2008, name="Dragging a Dead Deer Up a Hill")
    lib = Library([a1, a2, a3])
    for a in (a1, a2, a3):
        lib.set_tracks(a, tracks(a))
    return lib


class LibraryTests(unittest.TestCase):
    def test_artists_are_deduplicated_by_normalized_name(self):
        lib = sample_library()
        artists = lib.artists()
        self.assertEqual([a.key for a in artists], ["boards of canada", "grouper"])
        boc = lib.artist("boards of canada")
        self.assertEqual(boc.album_ids, ["al1", "al2"])  # release order
        self.assertEqual(len(lib.artist_tracks("boards of canada")), 6)

    def test_album_year_is_derived_from_tracks(self):
        a = album(1, year=None)
        lib = Library([a])
        lib.set_tracks(a, [replace(t, year=2005) for t in tracks(a)])
        self.assertEqual(lib.album("al1").year, 2005)

    def test_tracks_and_missing_albums(self):
        lib = Library([album(1), album(2)])
        lib.set_tracks(album(1), tracks(album(1), 2))
        self.assertIsNone(lib.tracks_of("al2"))
        self.assertEqual([a.id for a in lib.missing_track_albums()], ["al2"])
        self.assertEqual(len(lib.all_tracks()), 2)

    def test_search_finds_all_kinds_and_ranks_names_first(self):
        lib = sample_library()
        results = lib.search("campfire")
        self.assertEqual(results[0].kind, "album")
        self.assertEqual(results[0].item.id, "al2")
        # Tracks of that album match through the album field, ranked lower.
        self.assertTrue(all(r.kind == "track" for r in results[1:]))

        kinds = {r.kind for r in lib.search("grouper")}
        self.assertEqual(kinds, {"artist", "album", "track"})
        self.assertEqual(lib.search("grouper")[0].kind, "artist")

    def test_search_is_case_insensitive_and_multi_term(self):
        lib = sample_library()
        self.assertEqual(len([r for r in lib.search("DEAD deer") if r.kind == "album"]), 1)
        self.assertEqual(lib.search("   "), [])
        self.assertEqual(lib.search("zzz"), [])

    def test_search_index_updates_after_changes(self):
        lib = Library([album(1)])
        self.assertEqual(lib.search("song"), [])
        lib.set_tracks(album(1), tracks(album(1)))
        self.assertEqual(len(lib.search("song")), 3)

    def test_search_scales(self):
        albums = [album(i, f"Artist {i % 300}") for i in range(3000)]
        lib = Library(albums)
        for a in albums:
            lib.set_tracks(a, tracks(a, 10))
        lib.search("x")  # build index
        start = time.perf_counter()
        results = lib.search("artist 12 song")
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertTrue(results)

    def test_staleness(self):
        self.assertTrue(is_stale(Library(), 24))
        lib = Library([album(1)], synced_at=time.time())
        self.assertFalse(is_stale(lib, 24))
        lib.synced_at = time.time() - 25 * 3600
        self.assertTrue(is_stale(lib, 24))


class FakeClient:
    def __init__(self, albums: list[Album], cap: int = 500, ignore_offset: bool = False):
        self.albums, self.cap, self.ignore_offset = albums, cap, ignore_offset
        self.calls = 0

    def album_list_page(self, offset, size):
        self.calls += 1
        if self.ignore_offset:
            offset = 0
        return self.albums[offset: offset + min(size, self.cap)]

    def album(self, album_id):
        if album_id == "al2":
            raise NetworkError("Could not reach Bandcamp (timeout).")
        a = next(x for x in self.albums if x.id == album_id)
        return a, tracks(a)


class SyncTests(unittest.TestCase):
    def test_pagination_with_server_page_cap(self):
        albums = [album(i) for i in range(1234)]
        client = FakeClient(albums, cap=100)
        self.assertEqual(len(fetch_albums(client)), 1234)
        self.assertEqual(client.calls, 14)  # 13 pages + the empty one

    def test_pagination_stops_if_offset_is_ignored(self):
        client = FakeClient([album(i) for i in range(10)], cap=5, ignore_offset=True)
        self.assertEqual(len(fetch_albums(client)), 5)
        self.assertEqual(client.calls, 2)

    def test_fetch_tracks_reports_failures(self):
        albums = [album(i) for i in range(1, 5)]
        loaded = {}
        failures = fetch_tracks(FakeClient(albums), albums, lambda a, t: loaded.__setitem__(a.id, t))
        self.assertEqual(sorted(loaded), ["al1", "al3", "al4"])
        self.assertEqual([(a.id, "timeout" in msg) for a, msg in failures], [("al2", True)])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.path = Path(self._tmp.name) / "library.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        lib = sample_library()
        lib.synced_at = 1234.5
        Cache(self.path, "fan", "srv").save(lib)
        loaded = Cache(self.path, "fan", "srv").load()
        self.assertEqual(loaded.albums(), lib.albums())
        self.assertEqual(loaded.tracks_of("al1"), lib.tracks_of("al1"))
        self.assertEqual(loaded.synced_at, 1234.5)

    def test_missing_cache(self):
        self.assertIsNone(Cache(self.path, "fan", "srv").load())

    def test_other_account_is_ignored(self):
        Cache(self.path, "fan", "srv").save(sample_library())
        self.assertIsNone(Cache(self.path, "someone-else", "srv").load())

    def test_partial_sync_is_not_marked_fresh(self):
        Cache(self.path, "fan", "srv").save(sample_library(), complete=False)
        loaded = Cache(self.path, "fan", "srv").load()
        self.assertEqual(len(loaded), 3)
        self.assertIsNone(loaded.synced_at)
        self.assertTrue(is_stale(loaded, 24))

    def test_corrupt_cache_is_discarded(self):
        self.path.write_bytes(b"this is not a database" * 100)
        cache = Cache(self.path, "fan", "srv")
        self.assertIsNone(cache.load())
        cache.save(sample_library())  # and can be rebuilt
        self.assertEqual(len(cache.load()), 3)

    def test_incompatible_version_is_discarded(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {cache_mod.SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE albums (x)")
        conn.commit()
        conn.close()
        cache = Cache(self.path, "fan", "srv")
        self.assertIsNone(cache.load())
        cache.save(sample_library())
        self.assertEqual(len(cache.load()), 3)

    def test_bad_rows_are_discarded(self):
        Cache(self.path, "fan", "srv").save(sample_library())
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE albums SET data = 'not json'")
        conn.commit()
        conn.close()
        self.assertIsNone(Cache(self.path, "fan", "srv").load())


if __name__ == "__main__":
    unittest.main()
