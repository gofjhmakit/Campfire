# Campfire security notes

Audit date: October 2026. Regression tests for the items below are in
`tests/test_security.py`.

## What needs protecting

* **Subsonic credentials.** The Subsonic username and password generated in
  Bandcamp's Fan Settings. They are separate from the Bandcamp login and can
  be revoked there.
* **Request tokens.** Subsonic token auth sends `u`, `t = md5(password + salt)`
  and `s` in every URL. The server does not have to reject a reused salt, so
  anyone who sees one complete request URL can replay it against the whole
  API. **Treat any API or stream URL as a credential.**
* **Collection metadata.** What you own is private information.

## Measures

| Area | Measure |
|---|---|
| Credential storage | macOS Keychain (`security`; the password goes on stdin, never in argv), Secret Service (`secret-tool`, password on stdin), or `credentials.toml` written `0600` with `O_NOFOLLOW` in a `0700` directory. `repr()` of credential objects and the client masks the password. |
| Transport | API requests use urllib's default TLS verification. `server.url` must be `https://` (plain `http` only for `localhost`/`127.0.0.1`/`::1`; userinfo, query and fragment rejected). API redirects to non-https URLs are refused. |
| Streaming | Stream URLs are generated fresh per play and never cached. They are sent to mpv over its IPC socket, not on the command line, so they don't appear in `ps`. |
| mpv hardening | Always passed: `--tls-verify=yes` (older FFmpeg builds do not verify by default), `--ytdl=no` (the ytdl hook would run `yt-dlp <url>`), `--log-file=` (overrides a log file configured in `mpv.conf`), `--save-position-on-quit=no`. By default also `--no-config`, so the user's `mpv.conf` and **scripts** are not loaded: scripts such as scrobblers and presence plugins can read the current URL. Opt in with `playback.mpv_user_config = true`. |
| IPC socket | Lives in `$XDG_RUNTIME_DIR/campfire` or the cache directory, created `0700`. Commands are JSON-encoded; no shell is involved anywhere. |
| Local files | Library cache, cover image cache and their directories are `0600`/`0700`. Cached: album/track metadata, account name and server, and cover images. No credentials, tokens, stream URLs or audio. |
| Untrusted text | Everything from the server (titles, errors, field names) has control characters stripped before reaching the terminal, in both the TUI and the CLI (`check`, `login`), which prevents escape-sequence injection. |
| Untrusted data | JSON only (no pickle/eval). Responses are capped at 64 MB. Malformed objects are skipped. SQLite uses bound parameters. Theme files are data only, and theme names can't contain path separators. |
| Error messages | Never include URLs, tokens or passwords (tested). |
| Album art | Downloaded through the authenticated API like everything else (https only, 10 MB cap, must be `image/*`). It is decoded by the `ffmpeg` CLI, which parses untrusted data. The input format is forced from the file's magic bytes (JPEG/PNG/GIF/WebP only), and `-protocol_whitelist pipe` is set, so ffmpeg can't be steered into reading playlists, local files or URLs. ffmpeg runs without a shell, with a 15 s timeout. Cached files are named by a hash of the cover id (never a server-supplied path) and written `0600`/`O_NOFOLLOW`; a file that fails to decode is deleted. |
| Terminal graphics | Kitty graphics commands are sent with `q=2` on every chunk, so the terminal never sends replies that could be read as keystrokes. Only PNG data that Campfire created itself is sent. Images are shown through Unicode placeholder characters, which are ordinary text to curses and tmux. |

## Residual risks (not fixable in Campfire)

* **Token replay** is inherent to Subsonic token auth. A captured URL works
  until you regenerate the credentials in Bandcamp's settings. Bandcamp also
  sees, and may log, these URLs like any Subsonic server.
* **Same-user processes** can read your credentials. The Keychain item is
  created by the `security` tool, so other programs running as you can read
  it through `security` without a prompt. That's comparable to a `0600` file.
  Environment variable credentials (`CAMPFIRE_SUBSONIC_*`) are visible to
  processes of the same user.
* **MD5** in the token scheme is weak, but it's mandated by the Subsonic
  protocol. Your actual password is never sent.
* ffmpeg/libavcodec image and audio decoders process server data. That's the
  same exposure as any media player; keep mpv and ffmpeg up to date.
* `playback.mpv_path` and `playback.mpv_user_config = true` run whatever
  your configuration points to. That's your own config, so it's trusted by design.
* tmux's `allow-passthrough on` (needed for real album art in tmux) lets every
  program in a tmux pane send escape sequences straight to the outer terminal.
  That's a tmux-wide trade-off; Campfire works without it and falls back to
  text-block art.
* The `CAMPFIRE_CONFIG_DIR` / `CAMPFIRE_CACHE_DIR` overrides should point to
  dedicated directories, because Campfire sets them to `0700`.

## Verified manually (October 2026)

* With mpv 0.41 / FFmpeg 9, self-signed, expired and wrong-host certificates
  (badssl.com) are rejected.
* Bandcamp streaming works with `--no-config --tls-verify=yes`.
* The running mpv's arguments contain no URL or token.
