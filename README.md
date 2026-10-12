# Campfire

A terminal music player for your own Bandcamp collection. It uses Bandcamp's
official [Subsonic API](https://blog.bandcamp.com/2026/07/16/discover-improvements-and-subsonic-implementation/)
(open beta) and plays audio through [mpv](https://mpv.io).

```
  CAMPFIRE  Albums › Dummy Album One                                  / search  ? help

  LIBRARY           Dummy Album One  Test Artist · 2019 · 4 tracks                 3/5
    1: Artists
  ▶ 2: Albums           #  TITLE                                              TIME
    3: Tracks       ▶      Play album                                            00:24
                        1  Tone number 1                                         00:06
  PLAYBACK          ▶   2  Tone number 2                                         00:06
    4: Queue            3  Tone number 3                                         00:06

  ────────────────────────────────────────────────────────────────────────────────────
  ▶ Tone number 2                                                              vol 80%
    Test Artist — Dummy Album One    next · Tone number 3        █████████  ████████▌
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  00:01 / 00:06
  space play/pause   ←→ seek   n/p next/prev   1-4 sections   / search   ? help   q quit
```

* Browse your collection by artist, album or track. Search all three instantly.
* Play an album in order, start anywhere within it, or play everything by an artist.
* The playback queue auto-advances and skips tracks that can't be played.
* Black-and-orange default theme, plus file-based TOML themes.
* Library metadata is cached locally (SQLite) for instant startup.
* Real stereo level meter, measured from the decoded audio by mpv/ffmpeg.
* No Python dependencies beyond the standard library.

## Requirements

| | |
|---|---|
| OS | macOS or Linux |
| Python | 3.11 or newer (stdlib only; `curses` must be available, which it is on standard macOS/Linux builds) |
| mpv | Required for playback (tested with 0.41). The app still starts and browses without it. |
| Terminal | Any modern terminal. 256 colors recommended; 8 colors and monochrome work. Minimum 40×10. |

Install mpv:

```sh
brew install mpv            # macOS (Homebrew)
sudo apt install mpv        # Debian / Ubuntu
sudo dnf install mpv        # Fedora (RPM Fusion)
```

## Installation

From a checkout:

```sh
git clone <repository URL> Campfire
cd Campfire
pipx install --python python3.14 .     # any Python >= 3.11
```

This installs the `campfire` command into `~/.local/bin`. If that directory
isn't on your `PATH`, run `pipx ensurepath`. Install pipx with
`brew install pipx` or `sudo apt install pipx`. To make code changes take effect
without reinstalling, use `pipx install --editable …`. To update an existing
install, run `pipx reinstall campfire`.

On macOS, plain `pip3`/`python3` are usually Apple's Python 3.9, which is too
old (the install fails with a "requires a different Python" error), and
Homebrew's Python refuses `pip install` outside a virtual environment. That's
why pipx is used here. pip fetches `setuptools` to build the package; Campfire
itself has no runtime Python dependencies.

You can also run it from the checkout without installing: `python3 -m campfire`.

## Bandcamp setup (first run)

1. Sign in at bandcamp.com and open **Settings → Fan**
   (<https://bandcamp.com/settings?pane=fan>).
2. Scroll to **Subsonic** and generate credentials. These are a separate
   username and password just for Subsonic clients, not your Bandcamp login.
3. Run:

   ```sh
   campfire login
   ```

   Paste the username and password. Campfire strips stray whitespace (the
   settings page has been known to copy a leading space), checks the
   credentials with an authenticated request, and stores them:

   * macOS: in the Keychain (service `campfire-bandcamp-subsonic`)
   * Linux: in the Secret Service keyring via `secret-tool`, if available
   * otherwise: in `~/.config/campfire/credentials.toml` with permissions `0600`

4. Start the player:

   ```sh
   campfire
   ```

   The first start downloads your collection's metadata: one album list, then
   each album's track listing. Albums appear immediately and tracks fill in as
   they load. Later starts use the cache and refresh it in the background
   once it is older than 24 hours.

To remove stored credentials, run `campfire logout`. To check your setup and
see what Bandcamp's API currently supports, run `campfire check`. It makes
read-only requests and prints no secrets.

For scripting, the environment variables `CAMPFIRE_SUBSONIC_USER` and
`CAMPFIRE_SUBSONIC_PASSWORD` override stored credentials.

## Keyboard shortcuts

Press `?` in the app for this list. These are the defaults; see
[Custom key bindings](#custom-key-bindings) to change them.

| Key | Action |
|---|---|
| `Space` | Play / pause (when stopped, restarts the current queue track) |
| `←` `→` | Seek backward / forward (10 s by default) |
| `n` / `p` | Next / previous track (`p` restarts the track after the first 3 s) |
| `s` | Stop |
| `+` / `-` | Volume up / down |
| `.` | Go to the album that is playing |
| `↑` `↓` / `j` `k` | Move |
| `PgUp` `PgDn`, `g` `G`, `Home` `End` | Page, top / bottom |
| `Enter` | Open, or play from the selected track |
| `Esc` / `Backspace` / `h` | Back |
| `1` `2` `3` `4` | Artists, Albums, Tracks, Queue |
| `/` | Search |
| `r` | Refresh the library from Bandcamp |
| `q`, `Ctrl+C` | Quit |

The section keys are shown next to each section: in the sidebar on wide
terminals, and in a tab row under the header on narrow ones.

In search, everything you type goes into the query (including `space`, `q`,
digits and letters). `↑` `↓` select a result, `Enter` opens it, `Ctrl+U` clears the
query and `Esc` returns to the previous view.

What `Enter` does:

* **Album row:** opens the album.
* **"Play album" / "Play all tracks":** starts playback.
* **Track in an album:** plays from that track to the end of the album.
* **Track in the Tracks list or search:** a search result opens its album with
  the track selected; in the Tracks list it plays that track and the rest of
  its album.
* **Queue entry:** jumps to that entry.

## Configuration

`campfire paths` prints the actual locations.

| What | Where |
|---|---|
| Settings | `~/.config/campfire/config.toml` (or `$XDG_CONFIG_HOME/campfire`) |
| User themes | `~/.config/campfire/themes/` |
| Library cache | `~/.cache/campfire/library.sqlite3` (or `$XDG_CACHE_HOME/campfire`) |

A commented `config.toml` is created on first run:

```toml
[ui]
theme = "theme_default.toml"     # file in the themes dir, or a built-in theme
visualizer = true                # level meter in Now Playing

[playback]
seek_seconds = 10
volume = 80                      # initial volume, 0-100
previous_restart_seconds = 3.0   # "previous" restarts the track after this
mpv_path = "mpv"

[library]
cache_max_age_hours = 24

[server]
url = "https://bandcamp.com/api/subsonic"
```

Invalid values are reported in the status line and replaced by defaults. A
broken file never stops the app from starting.

## Custom key bindings

Add a `[keys]` table to `config.toml`. Each action takes one key or a list of
keys:

```toml
[keys]
artists = "a"
albums = ["l", "f2"]
play_pause = ["space", "f5"]
```

A key is a single character (`"n"`, `"G"` and `"g"` are different, `"1"`,
`"/"`), or one of `space`, `tab`, `left`, `right`, `insert`, `delete`,
`f1`–`f12`.

The actions and their defaults:

| Action | Default | | Action | Default |
|---|---|---|---|---|
| `play_pause` | `space` | | `artists` | `1` |
| `seek_back` | `left` | | `albums` | `2` |
| `seek_forward` | `right` | | `tracks` | `3` |
| `next` | `n` | | `queue` | `4` |
| `previous` | `p` | | `search` | `/` |
| `stop` | `s` | | `refresh` | `r` |
| `volume_up` | `+`, `=` | | `help` | `?` |
| `volume_down` | `-` | | `quit` | `q` |
| `goto_playing` | `.` | | `move_up` / `move_down` | `k` / `j` |
| `top` / `bottom` | `g` / `G` | | `back` | `h` |

The arrow keys `↑` `↓`, Page Up/Down, Home/End, Enter, Esc and Backspace always
keep their navigation meaning and can't be rebound, so you can't lock
yourself out.

The sidebar, section tabs, hint line and help screen show whatever keys are
configured. Binding a key that another action uses by default moves it to your
action, and the other action loses that key. Invalid names, reserved keys
and conflicts between your own bindings are reported in the status line, and
the defaults are used instead.

In search, printable keys always type into the query, so single-character
bindings don't fire while you type.

## Themes

Built-in themes are `theme_default.toml` (black and orange) and
`theme_dracula.toml`. To make your own, put a file in
`~/.config/campfire/themes/` and select it with `ui.theme`:

```toml
name = "My theme"

[colors]
background     = "#000000"   # or "default" for the terminal's own background
text           = "#d0d0d0"
text_secondary = "#8a8a8a"
muted          = "#585858"
accent         = "#ff8700"
selection_fg   = "#ffaf5f"
selection_bg   = "#262626"
current_track  = "#ff8700"
progress_fill  = "#ff8700"
progress_empty = "#303030"
border         = "#262626"
error          = "#ff5f5f"
warning        = "#ffd75f"
```

Any color you leave out is taken from the default theme. Colors are mapped to
the nearest of the terminal's 256 (or 8) colors. If a theme file is missing or
invalid, Campfire uses the default theme and shows a warning. Themes are plain
data and can't run code.

## Running the tests

```sh
python3 -m unittest
```

The automated tests mock all network and mpv interaction and need no
credentials, network access or audio device. They cover configuration, themes,
model parsing, the API client's error handling, library search and
pagination, the cache, the queue, the playback controller's state machine, and
UI layout logic.

### Manual verification

These are not automated:

1. `campfire check` reports what Bandcamp's API supports for your account.
2. `campfire`: play an album, pause/resume, seek, next/previous, let a
   track end and check that the next one starts, quit with `q` and with
   `Ctrl+C`, and confirm the terminal is restored.
3. Resize the terminal while running, down to below 40×10.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Bandcamp rejected the Subsonic credentials" | Regenerate them in Fan Settings → Subsonic and run `campfire login` again. Bandcamp's beta answers bad credentials with an empty HTTP 500, so this message can also appear during a Bandcamp outage. |
| "mpv not found" | Install mpv, or set `playback.mpv_path` to its full path. |
| "Could not reach Bandcamp" | Check your network. The cached library stays browsable. |
| A track won't play | Campfire shows mpv's error and moves on. After three failures in a row it stops. |
| "This stream does not support seeking" | mpv reported the stream as not seekable. |
| Wrong or ugly colors | Use a 256-color terminal (`TERM=xterm-256color`), or try `background = "default"` in a theme. |
| Stale library | Press `r`, or delete the cache file shown by `campfire paths`. |

## Known limitations

* Bandcamp's Subsonic API is a beta. Behavior described in
  [docs/technical-plan.md](docs/technical-plan.md) may change.
* The first sync makes one request per album (4 run in parallel). A small
  collection syncs in a couple of seconds; very large collections have not been
  tested and will take longer. Bandcamp itself warns the beta can be slow with big collections.
* Streams are whatever Bandcamp's Subsonic `stream` endpoint serves (MP3 from
  Bandcamp's CDN). There is no offline download.
* Playlists, artwork, scrobbling and gapless playback are not implemented.
* Artists are grouped by name, so two different artists with the same name
  are shown together.
* The level meter needs mpv built with libavfilter (standard builds have it).
  It is a level meter, not a spectrum analyzer.
* Windows is not supported (curses, Unix sockets).
