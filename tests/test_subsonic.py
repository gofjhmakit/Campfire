import hashlib
import io
import json
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from unittest import mock

from campfire.models import format_duration, parse_album, parse_items, parse_track, sort_album_tracks
from campfire.subsonic import (AuthError, EmptyServerError, NetworkError, NotFoundError,
                               NotSupportedError, SubsonicClient, SubsonicError)

PASSWORD = "s3cret-pass"


def ok(**body):
    return json.dumps({"subsonic-response": {"status": "ok", "version": "1.16.1", **body}}).encode()


def failed(code, message):
    return json.dumps({"subsonic-response": {"status": "failed",
                                             "error": {"code": code, "message": message}}}).encode()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b"", headers=None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://example/rest", code, "error", msg, io.BytesIO(body))


class FakeOpener:
    """Returns queued responses (bytes) or raises queued exceptions; records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def client(*responses):
    opener = FakeOpener(*responses)
    return SubsonicClient("https://bandcamp.com/api/subsonic/", "fan", PASSWORD, opener=opener), opener


class ClientTests(unittest.TestCase):
    def test_token_auth_parameters(self):
        c, opener = client(ok())
        c.call("getAlbumList2", type="newest")
        url = opener.requests[0].full_url
        self.assertTrue(url.startswith("https://bandcamp.com/api/subsonic/rest/getAlbumList2.view?"))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(q["u"], "fan")
        self.assertEqual(q["t"], hashlib.md5((PASSWORD + q["s"]).encode()).hexdigest())
        self.assertEqual((q["v"], q["c"], q["f"], q["type"]), ("1.16.1", "campfire", "json", "newest"))
        self.assertNotIn("p", q)
        self.assertNotIn(PASSWORD, url)

    def test_salt_changes_per_request(self):
        c, _ = client()
        self.assertNotEqual(c.stream_url("1"), c.stream_url("1"))

    def test_error_codes_are_mapped(self):
        cases = [(40, AuthError), (41, AuthError), (44, AuthError), (30, NotSupportedError),
                 (70, NotFoundError), (0, SubsonicError)]
        for code, exc_type in cases:
            c, _ = client(failed(code, "nope"))
            with self.assertRaises(exc_type):
                c.call("getAlbum", id="1")

    def test_empty_500_is_reported_and_verify_treats_it_as_auth(self):
        c, _ = client(http_error(500))
        with self.assertRaises(EmptyServerError):
            c.call("getAlbumList2")
        c, _ = client(http_error(500))
        with self.assertRaises(AuthError):
            c.verify()

    def test_bad_version_body_means_unsupported(self):
        c, _ = client(b"bad version")
        with self.assertRaises(NotSupportedError):
            c.call("getArtists")
        c, _ = client(http_error(400, b"bad version"))
        with self.assertRaises(NotSupportedError):
            c.call("getArtists")

    def test_http_statuses(self):
        c, _ = client(http_error(401))
        with self.assertRaises(AuthError):
            c.call("getAlbum")
        c, _ = client(http_error(404))
        with self.assertRaises(NotSupportedError):
            c.call("getAlbum")

    def test_network_errors(self):
        for exc in (urllib.error.URLError("no route"), TimeoutError(), ConnectionResetError()):
            c, _ = client(exc)
            with self.assertRaises(NetworkError):
                c.call("getAlbum")

    def test_invalid_json(self):
        c, _ = client(b"<html>oops</html>")
        with self.assertRaises(SubsonicError):
            c.call("getAlbum")

    @mock.patch("campfire.subsonic.time.sleep")
    def test_429_is_retried_once(self, sleep):
        c, opener = client(http_error(429, headers={"Retry-After": "1"}), ok())
        c.call("getAlbum")
        self.assertEqual(len(opener.requests), 2)
        sleep.assert_called_once_with(1.0)

    def test_errors_never_contain_secrets(self):
        responses = [http_error(500), http_error(401), urllib.error.URLError("x"), failed(40, "bad"),
                     b"junk", http_error(503)]
        for response in responses:
            c, opener = client(response)
            try:
                c.call("getAlbum", id="1")
            except SubsonicError as exc:
                text = str(exc)
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opener.requests[0].full_url).query))
                self.assertNotIn(PASSWORD, text)
                self.assertNotIn(q["t"], text)
                self.assertNotIn("?", text)
        self.assertNotIn(PASSWORD, repr(c))

    def test_album_parsing(self):
        body = ok(album={"id": "a1", "name": "Album", "artist": "Band", "year": 2021, "songCount": 2,
                         "song": [{"id": "t2", "title": "Two", "track": 2, "duration": 61},
                                  {"id": "t1", "title": "One", "track": 1, "duration": 3600}]})
        c, _ = client(body)
        album, tracks = c.album("a1")
        self.assertEqual(album.name, "Album")
        self.assertEqual([t.id for t in tracks], ["t1", "t2"])
        self.assertEqual(tracks[0].album_id, "a1")      # inherited from the album
        self.assertEqual(tracks[0].artist, "Band")

    def test_album_list_page(self):
        c, _ = client(ok(albumList2={"album": [{"id": "1", "name": "A", "artist": "X"}, {"bad": 1}]}))
        self.assertEqual([a.id for a in c.album_list_page(0, 500)], ["1"])
        c, _ = client(ok(albumList2={}))
        self.assertEqual(c.album_list_page(0, 500), [])


class ModelTests(unittest.TestCase):
    def test_parse_tolerates_missing_fields(self):
        album = parse_album({"id": 5})
        self.assertEqual((album.id, album.name, album.artist), ("5", "Unknown album", "Unknown artist"))
        track = parse_track({"id": "t", "track": "3", "duration": 12.7})
        self.assertEqual((track.title, track.track_no, track.duration), ("Untitled", 3, 12))

    def test_parse_items_skips_malformed(self):
        items = [{"id": "1"}, {"title": "no id"}, "junk", {"id": "2"}]
        self.assertEqual([a.id for a in parse_items(items, parse_album)], ["1", "2"])
        self.assertEqual(len(parse_items({"id": "1"}, parse_album)), 1)
        self.assertEqual(parse_items(None, parse_album), [])

    def test_track_sorting_by_disc_and_number(self):
        ts = [parse_track({"id": i, "track": n, "discNumber": d})
              for i, n, d in [("a", 1, 2), ("b", 2, 1), ("c", None, 1), ("d", 1, 1)]]
        self.assertEqual([t.id for t in sort_album_tracks(ts)], ["d", "b", "c", "a"])

    def test_format_duration(self):
        self.assertEqual(format_duration(None), "--:--")
        self.assertEqual(format_duration(65), "01:05")
        self.assertEqual(format_duration(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main()
