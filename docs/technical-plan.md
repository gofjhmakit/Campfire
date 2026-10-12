# Campfire – technical plan and research notes

Status: October 2026. Items marked **(verified)** were tested directly; items
marked **(reported)** come from third-party testing and have not yet been
confirmed against a live account by this project.

## 1. Bandcamp Subsonic API

Announcement: <https://blog.bandcamp.com/2026/07/16/discover-improvements-and-subsonic-implementation/>
(open beta; Bandcamp names Amperfy, Feishin and Submariner as supported clients).
There is no Bandcamp-specific reference documentation; the protocol reference is
<https://opensubsonic.netlify.app> / <http://www.subsonic.org/pages/api.jsp>.

| Topic | Finding |
|---|---|
| Endpoint | `https://bandcamp.com/api/subsonic/rest/<method>.view` **(verified)** |
| Server identity | `type=BandcampServer`, `serverVersion=1.0`, API `1.16.1`, `openSubsonic=true` **(verified)** |
| Response format | XML by default; `f=json` honoured **(verified)**. An earlier Music Assistant report of JSON being ignored is outdated. |
| Credentials | Dedicated Subsonic username/password generated under Fan Settings → Subsonic, separate from the Bandcamp login. |
| Auth scheme | Standard token auth: `u`, `t = md5(password + salt)`, `s` **(verified)** |
| Credential validation | `ping` returns `ok` even without credentials **(verified)**, so it cannot validate them. Campfire validates with `getAlbumList2?size=1`. |
| Auth failure | Every non-ping call without/with wrong credentials returns a bare **HTTP 500 with an empty body** – no Subsonic error envelope **(verified)**. Campfire maps this to "credentials rejected" when verifying and to a hinting error elsewhere. |
| Copy/paste quirk | Copied credentials can contain a leading space **(reported)**; Campfire strips whitespace. |
| Unsupported endpoints | Reported to answer with the plain body `bad version` **(reported)**; mapped to `NotSupportedError`. `getRandomSongs` returns an unparseable response **(verified)**; Campfire does not use it. |
| Collection | `getAlbumList2` with `alphabeticalByName`, `newest`, `recent`, `random`, `frequent` works; `offset` is honoured **(verified)**. Campfire advances `offset` by the number of albums actually returned, stops on an empty page, and stops if a page repeats. |
| Album metadata | `artist, coverArt, created, duration, genre, id, name, playCount, songCount` – **no `year`** **(verified)**. Campfire derives the album year from its tracks. `created` is an RFC 1123 date string, not ISO 8601. |
| Tracks | `getAlbum` returns songs with `album, albumId, artist, artistId, bitRate, contentType, coverArt, created, duration, genre, id, parent, size, suffix, title, track, year` **(verified)**. |
| IDs | Prefixed Bandcamp item IDs: albums `a:<number>`, tracks `t:<number>` **(verified)**. They are Bandcamp's own item IDs and are expected to be stable; long-term stability has not been observed. |
| Other endpoints | `getArtists`, `getArtist`, `getIndexes`, `getSong`, `search3`, `getMusicFolders`, `getPlaylists`, `getOpenSubsonicExtensions` all respond **(verified)**. `search3` with an empty query returned at most 100 songs. |
| Streaming | `stream.view` answers `302` to `t4.bcbits.com` with time-limited `token`/`ts` query parameters. The CDN serves `audio/mpeg`, supports `Range` (`206 Partial Content`), so **seeking works** **(verified)**. Stream URLs therefore must not be cached, and Campfire never does. |
| Performance | A small collection (under 20 albums) synced in about 2 seconds **(verified)**. Larger collections are untested. |

### Design consequences

* **Only IDs are cached, never stream URLs.** A fresh, freshly-salted
  `stream.view` URL is generated whenever a track starts, so any expiry of the
  redirect target does not matter. (The reference project, which scrapes
  Bandcamp pages, has to refresh expiring stream URLs; Subsonic avoids this.)
* **Artists are derived from albums** (grouped by normalised artist name), so
  the app does not depend on `getArtists` being implemented.
* **Search is local**, over cached metadata: instant, works offline, and does
  not depend on `search3`.
* **Library sync**: one paged album listing, then `getAlbum` per album with 4
  concurrent workers. Results stream into the UI as they arrive; opening an
  album that has not been fetched yet fetches it immediately.

## 2. Reference project (eremef/bandcamp-player)

Electron/TypeScript desktop app. It does **not** use Subsonic: it logs in with
cookies, scrapes collection pages and uses undocumented endpoints
(`/api/fancollection/1/...`, mobile API `streaming_url["mp3-128"]`), refreshing
expiring stream URLs. Useful lessons taken: cache metadata in SQLite with a TTL,
keep stream URLs out of the cache, and keep the player service separate from the UI.
Nothing else is reused; Campfire uses only the official Subsonic interface.

## 3. Playback

**Decision: mpv, controlled over its JSON IPC socket** (<https://mpv.io/manual/stable/#json-ipc>).

| Option | Verdict |
|---|---|
| mpv + JSON IPC | Chosen. Headless (`--idle --no-video --no-terminal`), streams HTTP(S) with caching, and reports everything over a socket: pause, position, duration, seekability, buffering, end-of-file with reason, and errors. |
| ffplay | No control interface (only its own keyboard input); can't report position or end reasons. |
| afplay (macOS) | Local files only, no control. |
| Pure Python | Would need an MP3 decoder and audio output libraries; contradicts the minimal-dependency goal. |

All required capabilities were **verified with mpv 0.41** against a local file
and a local HTTP stream: start/stop, pause/resume, relative/absolute seek,
position/duration, volume, `end-file` with `reason=eof|error`, playlist entry IDs
(used to ignore end events of replaced files), and process exit detection.

Stream URLs (which contain an auth token) are sent over the socket, never on
mpv's command line, so they are not visible in `ps`. mpv's own logging is disabled.

### Visualisation

mpv exposes no audio samples, only per-frame filter metadata (`af-metadata/<label>`).
Campfire inserts one lavfi graph (`playback/mpv.py: analysis_filter`):

```
aformat=stereo → asplit → [main] untouched stereo
                       → [ana] mono mix → asplit N → bandpass(f_i, Q=2.5) ×N
amerge(N bands + main) → asetnsamples=2048 → astats(RMS per channel) → pan back to the main stereo channels
```

* `amerge` drops frame metadata (verified with FFmpeg 9), so `astats` must run
  *after* merging. `pan` keeps the metadata.
* Channels 1..N are the spectrum and N+1/N+2 the stereo levels for the meter.
* **Verified:** with 100 Hz + 3 kHz test tones, the 89 Hz and 3238 Hz bands are
  the loudest (an automated test reproduces this with ffmpeg), and the output
  audio level is unchanged.
* A failing filter graph makes mpv reject the file, so on a playback error
  while the filter is active, the controller removes it (`af remove @viz`)
  and retries the track once.
* Measured CPU: about 3% (UI) plus 5–8% (mpv, decoding included) of one core
  while playing; 0 when paused.
* No oscilloscope: it would need raw samples.

### Album art

`getCoverArt` works (JPEG of about 47 KB served from the bcbits CDN; the `size`
parameter is ignored). The stdlib cannot decode JPEG, so the optional `ffmpeg`
CLI crops and scales each cover once to a 64×64 RGB grid (half-block
rendering, box-filtered per size) and a 480×480 PNG (kitty graphics
protocol).

Real images use the kitty protocol's **Unicode placeholders**: the PNG is
uploaded once per (cover, slot) with a virtual placement (`a=p,U=1`). The cells
are then drawn through curses as U+10EEEE plus row/column diacritics, with the
image id as the 256-color foreground (ids 16–255, because tmux re-encodes 0–15
as basic SGR colors). That's why it works unchanged inside tmux, where upload
commands go through passthrough (`\ePtmux;…`), and why curses redraws, the
help overlay and scrolling need no special handling. **Verified:** ncurses and
tmux 3.7 treat a placeholder as exactly one cell wide, and tmux forwards the
image id as `38;5;N`. **Verified by the user:** real covers render in Ghostty
1.3.1 inside tmux 3.7 with `allow-passthrough on`, both in the album view and as
the Now Playing thumbnail (the README screenshot).

## 4. Terminal UI

**Decision: standard-library `curses`.** It provides colour pairs, wide-character
output (`ncursesw`), key decoding, and diff-based screen updates, which is
enough for this layout. Rich/Textual/urwid were not needed.

* The main loop sleeps in `select()` on stdin plus a self-pipe that background
  threads (sync, mpv reader) write to, so the app uses no CPU when idle and
  reacts immediately to events. Redraws are capped at 20 fps.
* Theme colours are given as hex and mapped to the nearest xterm-256 colour
  (indices 16–255, never the user-configurable 0–15), or to the 8 basic colours
  on limited terminals. Monochrome terminals get reverse/bold/dim.
* Display width handles East Asian wide characters (common on Bandcamp).
* Layout degrades: sidebar ≥ 84 columns, key hints ≥ 16 rows, a 2-line Now
  Playing below 14 rows, and a "terminal too small" message below 40×10.
* Terminal restoration: `curses.wrapper` plus SIGTERM/SIGHUP → `SystemExit`,
  Ctrl+C handled, mpv shut down in a `finally`.

## 5. Storage

* Config: `$XDG_CONFIG_HOME/campfire/config.toml` (default `~/.config/campfire`),
  generated with comments on first run. TOML via the stdlib `tomllib`.
* Themes: built-in in the package; user themes in `~/.config/campfire/themes/`.
* Cache: `$XDG_CACHE_HOME/campfire/library.sqlite3`. Schema version in
  `PRAGMA user_version`; a corrupt or incompatible file is deleted and rebuilt.
  The cache is tied to account + server and is refreshed in the background
  when older than `library.cache_max_age_hours` (default 24) or with `r`.
* Credentials: macOS Keychain (`security`, password passed via stdin), Linux
  Secret Service (`secret-tool`), else `credentials.toml` with mode 0600.
  `CAMPFIRE_SUBSONIC_USER`/`CAMPFIRE_SUBSONIC_PASSWORD` override for scripting.

## 6. Dependencies

| Dependency | Purpose | Why not stdlib | Required? |
|---|---|---|---|
| mpv (≥ 0.33; tested 0.41) | Audio decoding, streaming, output | No stdlib audio playback | Required for playback; browsing works without it |
| ffmpeg CLI | Decoding album art (JPEG) | No stdlib image decoder | Optional; no art without it |
| setuptools | Build backend for `pip install` | Packaging only | Build-time only |
| `security` / `secret-tool` | Secure credential storage | No stdlib keyring | Optional; file fallback |

Python runtime dependencies: **none**.
