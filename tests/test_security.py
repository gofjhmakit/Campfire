"""Regression tests for the security hardening (see docs/security.md)."""

import io
import os
import stat
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from campfire import credentials
from campfire.__main__ import say
from campfire.cache import Cache
from campfire.config import DEFAULT_SERVER_URL, is_safe_server_url, load_settings
from campfire.library import Library
from campfire.playback.mpv import SAFETY_OPTIONS, MpvBackend
from campfire.subsonic import (MAX_RESPONSE_BYTES, NetworkError, SubsonicClient, SubsonicError,
                               _SecureRedirectHandler)

from .helpers import album, temp_dir


class MpvOptionsTests(unittest.TestCase):
    def args(self, **kw):
        return MpvBackend(None, Path("/run"), **kw).command_line("/usr/bin/mpv", Path("/run/s.sock"))

    def test_safety_options_are_always_passed(self):
        for user_config in (False, True):
            args = self.args(user_config=user_config)
            for option in ("--tls-verify=yes", "--ytdl=no", "--log-file=", "--save-position-on-quit=no"):
                self.assertIn(option, args)
        self.assertEqual(set(SAFETY_OPTIONS) - set(self.args()), set())

    def test_user_config_and_scripts_are_off_by_default(self):
        self.assertIn("--no-config", self.args())
        self.assertNotIn("--no-config", self.args(user_config=True))

    def test_no_secret_on_command_line(self):
        # Stream URLs are sent over the IPC socket, never as arguments.
        self.assertFalse(any("://" in a or "?" in a for a in self.args()))


class ServerUrlTests(unittest.TestCase):
    def test_validation(self):
        good = ["https://bandcamp.com/api/subsonic", "http://127.0.0.1:8765", "http://localhost:1"]
        bad = ["http://127.0.0.1.evil.com", "http://localhost.evil.com/", "http://evil.com",
               "https://user:pw@example.com", "ftp://example.com", "https://example.com:99999",
               "https://example.com/?x=1", "javascript:alert(1)", "https://", ""]
        for url in good:
            self.assertTrue(is_safe_server_url(url), url)
        for url in bad:
            self.assertFalse(is_safe_server_url(url), url)

    def test_unsafe_config_falls_back_to_bandcamp(self):
        with temp_dir() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[server]\nurl = "http://127.0.0.1.evil.com/api"\n')
            settings, warnings = load_settings(path)
            self.assertEqual(settings.server_url, DEFAULT_SERVER_URL)
            self.assertTrue(warnings)


class HttpTests(unittest.TestCase):
    def test_https_to_http_redirect_is_refused(self):
        handler = _SecureRedirectHandler()
        req = urllib.request.Request("https://bandcamp.com/api/subsonic/rest/getAlbum.view?t=x")
        with self.assertRaises(NetworkError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/steal")
        ok = handler.redirect_request(req, None, 302, "Found", {}, "https://bandcamp.com/other")
        self.assertEqual(ok.full_url, "https://bandcamp.com/other")

    def test_oversized_response_is_rejected(self):
        class Big(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1):
                return b"x" * (MAX_RESPONSE_BYTES + 1) if n == MAX_RESPONSE_BYTES + 1 else b""

        opener = mock.Mock()
        opener.open.return_value = Big()
        client = SubsonicClient("https://bandcamp.com/api/subsonic", "u", "p", opener=opener)
        with self.assertRaises(SubsonicError):
            client.call("getAlbumList2")


class OutputSanitizingTests(unittest.TestCase):
    def test_cli_output_strips_terminal_escapes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            say("ok\x1b]0;pwned\x07 album\x1b[2J\nline2\x9b31m")
        out = buf.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x9b", out)
        self.assertEqual(out.count("\n"), 2)  # intentional newlines are kept


class FilePermissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_cache_is_private(self):
        path = self.root / "cache" / "library.sqlite3"
        Cache(path, "fan", "srv").save(Library([album(1)]))
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.mode(path.parent), 0o700)

    def test_credentials_file_is_private_and_refuses_symlinks(self):
        with mock.patch.dict(os.environ, {"CAMPFIRE_CONFIG_DIR": str(self.root / "cfg")}):
            path = credentials.credentials_file()
            credentials._file_save("fan", "pw")
            self.assertEqual(self.mode(path), 0o600)
            self.assertEqual(credentials._file_load().password, "pw")

            path.unlink()
            target = self.root / "elsewhere.txt"
            target.write_text("")
            path.symlink_to(target)
            with self.assertRaises(credentials.CredentialError):
                credentials._file_save("fan", "pw")
            self.assertEqual(target.read_text(), "")  # nothing written through the link

    def test_existing_world_readable_credentials_file_is_tightened(self):
        with mock.patch.dict(os.environ, {"CAMPFIRE_CONFIG_DIR": str(self.root / "cfg")}):
            path = credentials.credentials_file()
            path.parent.mkdir(parents=True)
            path.write_text("")
            os.chmod(path, 0o644)
            credentials._file_save("fan", "pw")
            self.assertEqual(self.mode(path), 0o600)

    def test_credentials_repr_hides_password(self):
        self.assertNotIn("hunter2", repr(credentials.Credentials("fan", "hunter2")))
        self.assertNotIn("hunter2", repr(SubsonicClient("https://x.org", "fan", "hunter2")))


if __name__ == "__main__":
    unittest.main()
