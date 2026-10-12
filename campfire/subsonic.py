"""Minimal client for Bandcamp's Subsonic API, using only the standard library.

Observed Bandcamp behaviour (October 2026 beta), which shapes the error handling:
- `ping` answers "ok" even without credentials, so it cannot verify them.
- Any other request with missing or wrong credentials fails with a bare
  HTTP 500 and an empty body instead of a Subsonic error envelope.
- Unimplemented endpoints have been reported to answer with the plain-text
  body "bad version".
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import APP_NAME, __version__
from .models import Album, Track, parse_album, parse_items, parse_track, sort_album_tracks

API_VERSION = "1.16.1"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # a whole-collection listing is far smaller
MAX_IMAGE_BYTES = 10 * 1024 * 1024
USER_AGENT = f"Campfire/{__version__}"


class SubsonicError(Exception):
    """Base class. Messages are safe to show: they never contain credentials."""


class NetworkError(SubsonicError):
    pass


class AuthError(SubsonicError):
    pass


class NotSupportedError(SubsonicError):
    pass


class NotFoundError(SubsonicError):
    pass


class EmptyServerError(SubsonicError):
    """Bare HTTP 500 without a body: how Bandcamp's beta reports bad credentials."""


# Subsonic error codes: http://www.subsonic.org/pages/api.jsp#errors
_AUTH_CODES = {40, 41, 42, 43, 44, 50}
_UNSUPPORTED_CODES = {20, 30}


class SubsonicClient:
    def __init__(self, base_url: str, username: str, password: str,
                 timeout: float = 20.0, opener: urllib.request.OpenerDirector | None = None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(_SecureRedirectHandler)

    def __repr__(self) -> str:
        return f"SubsonicClient({self.base_url!r}, user={self.username!r})"

    # -- URL building ----------------------------------------------------------

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()
        return {"u": self.username, "t": token, "s": salt, "v": API_VERSION, "c": APP_NAME}

    def url(self, method: str, **params: object) -> str:
        query = self._auth_params()
        query.update({k: str(v) for k, v in params.items() if v is not None})
        return f"{self.base_url}/rest/{method}.view?{urllib.parse.urlencode(query)}"

    def stream_url(self, track_id: str) -> str:
        """Authenticated stream URL. Contains a token: never log or display it."""
        return self.url("stream", id=track_id)

    # -- Requests --------------------------------------------------------------

    def call(self, method: str, **params: object) -> dict:
        """Perform a request and return the body of `subsonic-response`."""
        request = urllib.request.Request(
            self.url(method, f="json", **params),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        for attempt in range(2):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SubsonicError(f"Response from Bandcamp for '{method}' is too large.")
                break
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if exc.code == 429 and attempt == 0:
                    exc.close()
                    time.sleep(_retry_delay(retry_after))
                    continue
                raise self._http_error(method, exc) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise NetworkError(f"Could not reach Bandcamp ({reason}).") from None
        return self._parse(method, body)

    def _http_error(self, method: str, exc: urllib.error.HTTPError) -> SubsonicError:
        try:
            body = exc.read(512).decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        finally:
            exc.close()
        if body == "bad version":
            return NotSupportedError(f"Bandcamp does not support '{method}'.")
        if exc.code in (401, 403):
            return AuthError("Bandcamp rejected the credentials.")
        if exc.code == 404:
            return NotSupportedError(f"Bandcamp does not support '{method}' (HTTP 404).")
        if exc.code == 500 and not body:
            # Bandcamp's beta answers bad credentials this way, but so might a
            # genuine server fault; callers that verify credentials treat it as auth.
            return EmptyServerError(
                f"Bandcamp returned an empty HTTP 500 for '{method}'. "
                "This usually means the Subsonic credentials are invalid.")
        return SubsonicError(f"Bandcamp returned HTTP {exc.code} for '{method}'.")

    @staticmethod
    def _parse(method: str, body: bytes) -> dict:
        text = body.decode("utf-8", "replace").strip()
        if text == "bad version":
            raise NotSupportedError(f"Bandcamp does not support '{method}'.")
        try:
            envelope = json.loads(text)["subsonic-response"]
        except (ValueError, KeyError, TypeError):
            raise SubsonicError(f"Unexpected response from Bandcamp for '{method}'.") from None
        if envelope.get("status") == "ok":
            return envelope
        error = envelope.get("error") or {}
        code = error.get("code")
        message = str(error.get("message") or "unknown error")
        if code in _AUTH_CODES:
            raise AuthError(f"Authentication failed: {message}")
        if code in _UNSUPPORTED_CODES:
            raise NotSupportedError(f"'{method}' not supported: {message}")
        if code == 70:
            raise NotFoundError(message)
        raise SubsonicError(f"Bandcamp error for '{method}': {message}")

    # -- API operations ----------------------------------------------------------

    def verify(self) -> None:
        """Check credentials with a real authenticated request (ping is not enough)."""
        try:
            self.call("getAlbumList2", type="alphabeticalByName", size=1)
        except EmptyServerError:
            raise AuthError("Bandcamp rejected the Subsonic credentials "
                            "(check them under Fan Settings → Subsonic).") from None

    def album_list_page(self, offset: int, size: int) -> list[Album]:
        body = self.call("getAlbumList2", type="alphabeticalByName", size=size, offset=offset)
        return parse_items((body.get("albumList2") or {}).get("album"), parse_album)

    def album(self, album_id: str) -> tuple[Album, list[Track]]:
        body = self.call("getAlbum", id=album_id)
        data = body.get("album")
        if not isinstance(data, dict):
            raise SubsonicError("Unexpected getAlbum response.")
        album = parse_album(data)
        tracks = sort_album_tracks(parse_items(data.get("song"), parse_track, album))
        return album, tracks

    def cover_art(self, cover_id: str, size: int | None = None) -> bytes:
        """Album artwork (Bandcamp serves JPEG). Raises SubsonicError."""
        request = urllib.request.Request(self.url("getCoverArt", id=cover_id, size=size),
                                         headers={"User-Agent": USER_AGENT})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._http_error("getCoverArt", exc) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise NetworkError(f"Could not reach Bandcamp ({getattr(exc, 'reason', exc)}).") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise SubsonicError("Cover art is too large.")
        if not content_type.startswith("image/"):
            self._parse("getCoverArt", data)  # a Subsonic error envelope: raises
            raise SubsonicError("Bandcamp did not return an image for the cover art.")
        return data

    def search(self, query: str, artist_count: int = 20, album_count: int = 20,
               song_count: int = 50) -> dict:
        return self.call("search3", query=query, artistCount=artist_count,
                         albumCount=album_count, songCount=song_count).get("searchResult3") or {}


class _SecureRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https (never downgrade a request to plain http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https" and \
                urllib.parse.urlsplit(req.full_url).scheme == "https":
            raise NetworkError("Bandcamp redirected to an insecure (http) address; refused.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_delay(header: str | None) -> float:
    try:
        return max(0.0, min(float(header), 10.0)) if header else 2.0
    except ValueError:
        return 2.0
