"""Command line entry point: `campfire [run|login|logout|check|paths]`."""

from __future__ import annotations

import argparse
import getpass
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, credentials
from .config import cache_dir, config_path, load_settings, themes_dir
from .subsonic import USER_AGENT, SubsonicClient, SubsonicError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="campfire", description="Terminal player for your Bandcamp collection.")
    parser.add_argument("--version", action="version", version=f"campfire {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the player (default)")
    sub.add_parser("login", help="store and verify your Bandcamp Subsonic credentials")
    sub.add_parser("logout", help="remove stored credentials")
    sub.add_parser("check", help="verify credentials and report what the Bandcamp API supports")
    sub.add_parser("paths", help="show configuration, theme and cache locations")
    args = parser.parse_args(argv)

    command = args.command or "run"
    try:
        return {"run": cmd_run, "login": cmd_login, "logout": cmd_logout,
                "check": cmd_check, "paths": cmd_paths}[command]()
    except KeyboardInterrupt:
        print()
        return 130


def _client() -> SubsonicClient | None:
    settings, _ = load_settings()
    creds = credentials.load()
    if not creds:
        print("No Bandcamp Subsonic credentials found. Run `campfire login` first.", file=sys.stderr)
        return None
    return SubsonicClient(settings.server_url, creds.username, creds.password)


def cmd_run() -> int:
    from .ui.tui import run  # curses is only needed for the player itself
    return run()


def cmd_login() -> int:
    settings, _ = load_settings()
    print("Create Subsonic credentials on Bandcamp: Fan Settings → Subsonic")
    print("  https://bandcamp.com/settings?pane=fan\n")
    username = credentials.clean(input("Subsonic username: "))
    password = credentials.clean(getpass.getpass("Subsonic password (hidden): "))
    if not username or not password:
        print("Username and password are required.", file=sys.stderr)
        return 1
    print("Checking credentials with Bandcamp…")
    try:
        SubsonicClient(settings.server_url, username, password).verify()
    except SubsonicError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    try:
        where = credentials.save(username, password)
    except credentials.CredentialError as exc:
        print(f"Could not store credentials: {exc}", file=sys.stderr)
        return 1
    print(f"Credentials verified and stored in {where}.")
    return 0


def cmd_logout() -> int:
    credentials.delete()
    print("Stored credentials removed.")
    return 0


def cmd_paths() -> int:
    creds = credentials.load()
    print(f"config:      {config_path()}")
    print(f"themes:      {themes_dir()}")
    print(f"cache:       {cache_dir()}")
    print(f"credentials: {creds.source if creds else 'not set'} (backend: {credentials.backend_name()})")
    return 0


def cmd_check() -> int:
    """Sanitized report of Bandcamp API behaviour. Prints no secrets or URLs with tokens."""
    client = _client()
    if not client:
        return 1

    def probe(label: str, method: str, **params):
        try:
            body = client.call(method, **params)
            print(f"  ok    {label}")
            return body
        except SubsonicError as exc:
            print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
            return None

    print("Credentials:")
    try:
        client.verify()
        print("  ok    authenticated request accepted")
    except SubsonicError as exc:
        print(f"  FAIL  {exc}")
        return 1

    print("\nAlbum list (getAlbumList2):")
    first = probe("size=500", "getAlbumList2", type="alphabeticalByName", size=500)
    albums = ((first or {}).get("albumList2") or {}).get("album") or []
    print(f"        returned {len(albums)} albums for size=500")
    a = probe("offset=0 size=3", "getAlbumList2", type="alphabeticalByName", size=3, offset=0)
    b = probe("offset=3 size=3", "getAlbumList2", type="alphabeticalByName", size=3, offset=3)
    ids = lambda body: [x.get("id") for x in ((body or {}).get("albumList2") or {}).get("album") or []]
    print(f"        offset honoured: {bool(ids(a)) and set(ids(a)).isdisjoint(ids(b))}")
    for list_type in ("newest", "recent", "random", "frequent"):
        probe(f"type={list_type}", "getAlbumList2", type=list_type, size=1)
    if albums:
        print(f"        album fields: {sorted(albums[0])}")

    album_id = albums[0]["id"] if albums else None
    song = None
    if album_id:
        print("\nAlbum detail (getAlbum):")
        body = probe("getAlbum", "getAlbum", id=album_id)
        songs = ((body or {}).get("album") or {}).get("song") or []
        print(f"        {len(songs)} tracks; song fields: {sorted(songs[0]) if songs else '-'}")
        print(f"        id formats: album={album_id!r} track={songs[0].get('id')!r}" if songs else "")
        song = songs[0] if songs else None

    print("\nOther endpoints:")
    probe("getOpenSubsonicExtensions", "getOpenSubsonicExtensions")
    probe("getMusicFolders", "getMusicFolders")
    artists = probe("getArtists", "getArtists")
    if artists:
        index = (artists.get("artists") or {}).get("index") or []
        count = sum(len(i.get("artist") or []) for i in index)
        print(f"        {count} artists")
        first_artist = next((x for i in index for x in i.get("artist") or []), None)
        if first_artist:
            res = probe("getArtist", "getArtist", id=first_artist["id"])
            if res:
                print(f"        artist fields: {sorted(res.get('artist') or {})}")
    probe("getIndexes", "getIndexes")
    if song:
        probe("getSong", "getSong", id=song["id"])
        word = (song.get("title") or "a").split()[0]
        res = probe("search3 (title word)", "search3", query=word, songCount=5)
        if res:
            r = res.get("searchResult3") or {}
            print(f"        artists={len(r.get('artist') or [])} albums={len(r.get('album') or [])} "
                  f"songs={len(r.get('song') or [])}")
        res = probe('search3 (empty query = everything?)', "search3", query="", songCount=500,
                    albumCount=0, artistCount=0)
        if res:
            print(f"        songs={len((res.get('searchResult3') or {}).get('song') or [])}")
    probe("getPlaylists", "getPlaylists")
    probe("getRandomSongs", "getRandomSongs", size=1)

    if song:
        print("\nStream (first track):")
        _probe_stream(client, song["id"])
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _probe_stream(client: SubsonicClient, track_id: str) -> None:
    headers = {"User-Agent": USER_AGENT, "Range": "bytes=0-1023"}
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(urllib.request.Request(client.stream_url(track_id), headers=headers), timeout=20)
        print(f"  HTTP {resp.status} (no redirect) type={resp.headers.get('Content-Type')}")
        return
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            print(f"  FAIL  HTTP {exc.code}")
            return
        status, location = exc.code, exc.headers.get("Location", "")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  FAIL  {exc}")
        return
    target = urllib.parse.urlsplit(location)
    query_keys = sorted(urllib.parse.parse_qs(target.query))
    print(f"  redirect {status} → host={target.hostname} query keys={query_keys}")
    try:
        with urllib.request.urlopen(urllib.request.Request(location, headers=headers), timeout=20) as r:
            r.read(1024)
            print(f"  HTTP {r.status} type={r.headers.get('Content-Type')} "
                  f"accept-ranges={r.headers.get('Accept-Ranges')} "
                  f"content-range={'yes' if r.headers.get('Content-Range') else 'no'} "
                  f"cache-control={r.headers.get('Cache-Control')} expires={r.headers.get('Expires')}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  FAIL  fetching redirect target: {exc}")


if __name__ == "__main__":
    sys.exit(main())
