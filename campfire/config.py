"""User configuration: locations, defaults and loading of config.toml."""

from __future__ import annotations

import os
import tomllib
import urllib.parse
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import APP_NAME

DEFAULT_SERVER_URL = "https://bandcamp.com/api/subsonic"


def config_dir() -> Path:
    if override := os.environ.get("CAMPFIRE_CONFIG_DIR"):
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def cache_dir() -> Path:
    if override := os.environ.get("CAMPFIRE_CACHE_DIR"):
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / APP_NAME


def runtime_dir() -> Path:
    """Directory for the mpv IPC socket (kept short: socket paths are length-limited)."""
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(xdg) / APP_NAME
    return cache_dir()


@dataclass
class Settings:
    # [ui]
    theme: str = "theme_default.toml"
    visualizer: bool = True
    # [playback]
    seek_seconds: int = 10
    volume: int = 80
    previous_restart_seconds: float = 3.0
    mpv_path: str = "mpv"
    mpv_user_config: bool = False
    # [library]
    cache_max_age_hours: float = 24.0
    # [server]
    server_url: str = DEFAULT_SERVER_URL
    # [keys]: action -> key or list of keys; validated by campfire.ui.keys
    keys: dict = field(default_factory=dict)


# Which section of config.toml each setting lives in.
SECTIONS: dict[str, tuple[str, ...]] = {
    "ui": ("theme", "visualizer"),
    "playback": ("seek_seconds", "volume", "previous_restart_seconds", "mpv_path",
                 "mpv_user_config"),
    "library": ("cache_max_age_hours",),
    "server": ("server_url",),
}

DEFAULT_CONFIG = """\
# Campfire configuration. Delete this file to regenerate the defaults.

[ui]
# Theme file name. Looked up in the user themes directory first
# ({themes_dir}), then in the built-in themes.
theme = "theme_default.toml"
# Show the audio level meter in the Now Playing panel.
visualizer = true

[playback]
# Seconds to jump with the left and right arrow keys.
seek_seconds = 10
# Initial volume, 0-100.
volume = 80
# "Previous" restarts the current track if it has played longer than this
# many seconds; otherwise it goes to the previous track in the queue.
previous_restart_seconds = 3.0
# mpv executable name or full path.
mpv_path = "mpv"
# Load your own mpv.conf and mpv scripts. Off by default: stream URLs contain
# your Subsonic auth token, and mpv scripts (scrobblers, presence plugins, ...)
# can read the current URL. Enable only if you trust all your mpv scripts.
mpv_user_config = false

[library]
# Refresh the cached library in the background when it is older than this.
cache_max_age_hours = 24

[server]
url = "{server_url}"

[keys]
# Rebind actions here. A value is one key or a list of keys: a single
# character ("n", "G", "1") or a name: space, tab, left, right, insert,
# delete, f1-f12. Arrow up/down, Page Up/Down, Home/End, Enter, Esc and
# Backspace always keep their navigation meaning. Defaults:
#
# play_pause = "space"      seek_back = "left"       seek_forward = "right"
# next = "n"                previous = "p"           stop = "s"
# volume_up = ["+", "="]    volume_down = "-"        goto_playing = "."
# artists = "1"             albums = "2"             tracks = "3"
# queue = "4"               search = "/"             refresh = "r"
# help = "?"                quit = "q"
# move_up = "k"             move_down = "j"          top = "g"
# bottom = "G"              back = "h"
"""


def config_path() -> Path:
    return config_dir() / "config.toml"


def themes_dir() -> Path:
    return config_dir() / "themes"


def write_default_config(path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DEFAULT_CONFIG.format(themes_dir=themes_dir(), server_url=DEFAULT_SERVER_URL),
        encoding="utf-8",
    )
    return path


def load_settings(path: Path | None = None, create: bool = True) -> tuple[Settings, list[str]]:
    """Load settings, falling back to defaults for anything missing or invalid.

    Returns the settings and a list of human-readable warnings.
    """
    path = path or config_path()
    settings = Settings()
    warnings: list[str] = []

    if not path.exists():
        if create:
            try:
                write_default_config(path)
            except OSError as exc:
                warnings.append(f"Could not create default config at {path}: {exc.strerror}")
        return settings, warnings

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return settings, [f"Could not read {path}: {exc}. Using defaults."]
    except tomllib.TOMLDecodeError as exc:
        return settings, [f"Invalid TOML in {path}: {exc}. Using defaults."]

    keys_table = data.get("keys", {})
    if isinstance(keys_table, dict):
        settings.keys = keys_table
    else:
        warnings.append(f"[keys] in {path} must be a table; ignored.")

    types = {f.name: f.type for f in fields(Settings)}
    for section, keys in SECTIONS.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            warnings.append(f"[{section}] in {path} must be a table; ignored.")
            continue
        for key, value in table.items():
            name = "server_url" if (section, key) == ("server", "url") else key
            if name not in keys:
                warnings.append(f"Unknown setting {section}.{key} ignored.")
                continue
            coerced = _coerce(value, types[name])
            if coerced is None:
                warnings.append(f"Invalid value for {section}.{key}: {value!r}; using default.")
                continue
            setattr(settings, name, coerced)

    for section in data:
        if section not in SECTIONS and section != "keys":
            warnings.append(f"Unknown section [{section}] ignored.")

    warnings.extend(_clamp(settings))
    return settings, warnings


def _coerce(value: object, type_name: str) -> object | None:
    if type_name == "bool":
        return value if isinstance(value, bool) else None
    if type_name == "int":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if type_name == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if type_name == "str":
        return value if isinstance(value, str) and value.strip() else None
    return None


def is_safe_server_url(url: str) -> bool:
    """Credentials are sent with every request, so require TLS except to loopback."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError for an invalid port
    except ValueError:
        return False
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1")


def _clamp(s: Settings) -> list[str]:
    warnings = []
    if not 0 <= s.volume <= 100:
        warnings.append("playback.volume must be 0-100; clamped.")
        s.volume = max(0, min(100, s.volume))
    if s.seek_seconds < 1:
        warnings.append("playback.seek_seconds must be at least 1; using 10.")
        s.seek_seconds = 10
    if s.previous_restart_seconds < 0:
        s.previous_restart_seconds = 0.0
    if s.cache_max_age_hours < 0:
        s.cache_max_age_hours = 0.0
    if not is_safe_server_url(s.server_url):
        warnings.append("server.url must be an https URL (plain http only for localhost); "
                        "using the Bandcamp default.")
        s.server_url = DEFAULT_SERVER_URL
    return warnings
