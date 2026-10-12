"""Tests for UI logic that does not need a real terminal: text fitting, views, app events."""

import os
import unittest
from unittest import mock

from campfire.app import App, EventBus
from campfire.config import Settings
from campfire.credentials import Credentials
from campfire.theme import default_theme
from campfire.ui import text as tx
from campfire.ui.views import AlbumsView, AlbumView, ArtistView, QueueView, SearchView

from .helpers import album, temp_dir, tracks


class TextTests(unittest.TestCase):
    def test_width_handles_wide_and_combining_characters(self):
        self.assertEqual(tx.width("abc"), 3)
        self.assertEqual(tx.width("坂本龍一"), 8)
        self.assertEqual(tx.width("é"), 1)

    def test_truncate_and_fit(self):
        self.assertEqual(tx.truncate("Hello world", 8), "Hello w…")
        self.assertEqual(tx.truncate("Hi", 8), "Hi")
        self.assertEqual(tx.truncate("坂本龍一", 5), "坂本…")
        self.assertEqual(tx.fit("ab", 4), "ab  ")
        self.assertEqual(tx.fit("ab", 4, "right"), "  ab")
        self.assertEqual(tx.width(tx.fit("坂本龍一abc", 6)), 6)

    def test_clean_removes_control_characters(self):
        self.assertEqual(tx.clean("a\x1b[2Jb\n"), "a [2Jb ")

    def test_column_widths(self):
        specs = [(12, 3), (10, 2), (4, 0), (6, 0)]
        widths = tx.column_widths(100, specs)
        self.assertEqual(sum(widths) + 2 * 3, 100)
        self.assertEqual(widths[2:], [4, 6])
        self.assertGreater(widths[0], widths[1])
        # Narrow: trailing columns are dropped rather than overflowing.
        self.assertEqual(tx.column_widths(30, specs), [12, 10, 4, 0])  # 12+10+4 + 2 gaps of 2
        narrow = tx.column_widths(25, specs)
        self.assertEqual(narrow[2:], [0, 0])
        self.assertEqual(sum(narrow) + 2, 25)
        self.assertEqual(tx.column_widths(5, specs)[0], 5)


class AppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        patcher = mock.patch.dict(os.environ, {"CAMPFIRE_CACHE_DIR": self._tmp.name,
                                               "CAMPFIRE_CONFIG_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = App(Settings(), default_theme(), Credentials("fan", "pw"))
        self.addCleanup(self.app.shutdown)
        self.app.client = mock.Mock()
        self.app.client.stream_url.side_effect = lambda track_id: f"url:{track_id}"
        self.backend = mock.Mock(running=True)
        self.app.player.backend = self.backend

    def tearDown(self):
        self._tmp.cleanup()

    def sync(self, albums, with_tracks=True):
        self.app.bus.put(("sync-albums", albums))
        if with_tracks:
            for a in albums:
                self.app.bus.put(("album-tracks", a, tracks(a)))
        self.app.bus.put(("sync-done", []))
        self.assertTrue(self.app.process_events())

    def test_sync_events_update_library_and_cache(self):
        self.sync([album(1), album(2, "Other")])
        self.assertEqual(len(self.app.library), 2)
        self.assertIsNotNone(self.app.library.synced_at)
        self.assertEqual(len(self.app.cache.load()), 2)
        self.assertIn("Library updated", self.app.current_notice().text)

    def test_refresh_keeps_known_tracks_until_replaced(self):
        self.sync([album(1)])
        self.app.bus.put(("sync-albums", [album(1), album(2)]))
        self.app.process_events()
        self.assertEqual(len(self.app.library.tracks_of("al1")), 3)
        self.assertIsNone(self.app.library.tracks_of("al2"))

    def test_sync_error_messages(self):
        from campfire.subsonic import EmptyServerError, NetworkError
        self.app.bus.put(("sync-error", EmptyServerError("x")))
        self.app.process_events()
        self.assertIn("campfire login", self.app.current_notice().text)
        self.app.notices.clear()
        self.app.bus.put(("sync-error", NetworkError("Could not reach Bandcamp (offline).")))
        self.app.process_events()
        self.assertIn("offline", self.app.current_notice().text)

    def test_play_album_from_track(self):
        a = album(1)
        self.sync([a])
        self.app.play_album(a.id, tracks(a)[1].id)
        self.backend.load.assert_called_with(f"url:{tracks(a)[1].id}")
        self.assertEqual(self.app.player.queue.upcoming(), tracks(a)[2:])

    def test_play_album_without_tracks_requests_them(self):
        self.sync([album(1)], with_tracks=False)
        self.app.client.album.return_value = (album(1), tracks(album(1)))
        self.app.play_album("al1")
        self.backend.load.assert_not_called()
        self.assertIn("Loading album", self.app.current_notice().text)

    def test_play_artist_plays_all_albums(self):
        self.sync([album(1, "Band"), album(2, "Band"), album(3, "Else")])
        self.app.play_artist("band")
        self.assertEqual(len(self.app.player.queue), 6)

    def test_views(self):
        a1, a2 = album(1, "Band"), album(2, "Band", name="Campfire Songs")
        self.sync([a1, a2])
        albums = AlbumsView()
        self.assertEqual(len(albums.rows(self.app)), 2)
        album_view = albums.activate(self.app, albums.rows(self.app)[0])
        self.assertIsInstance(album_view, AlbumView)
        rows = album_view.rows(self.app)
        self.assertEqual(rows[0].kind, "action")
        self.assertEqual(len(rows), 4)

        artist_view = ArtistView("band")
        rows = artist_view.rows(self.app)
        self.assertEqual([r.kind for r in rows], ["action", "album", "album"])
        artist_view.activate(self.app, rows[0])
        self.assertEqual(len(self.app.player.queue), 6)
        self.assertEqual(len(QueueView().rows(self.app)), 6)

        search = SearchView()
        search.query = "campfire song 2"
        rows = search.rows(self.app)
        self.assertEqual(rows[0].kind, "track")
        target = search.activate(self.app, rows[0])
        self.assertIsInstance(target, AlbumView)
        target_rows = target.rows(self.app)
        self.assertEqual(target_rows[target.cursor].track_id, rows[0].track_id)

    def test_notices_expire_and_deduplicate(self):
        self.app.notify("info", "hello")
        self.app.notify("info", "hello")
        self.assertEqual(len(self.app.notices), 1)
        self.app.notices[0].expires = 0
        self.assertIsNone(self.app.current_notice())


class EventBusTests(unittest.TestCase):
    def test_put_wakes_and_drains(self):
        import select
        bus = EventBus()
        self.addCleanup(bus.close)
        self.assertEqual(select.select([bus], [], [], 0)[0], [])
        bus.put(("a",))
        bus.put(("b",))
        self.assertEqual(select.select([bus], [], [], 0)[0], [bus])
        self.assertEqual(bus.drain(), [("a",), ("b",)])
        self.assertEqual(select.select([bus], [], [], 0)[0], [])


if __name__ == "__main__":
    unittest.main()
