"""File-based TOML themes.

A theme is a TOML file with an optional `name` and a `[colors]` table. Colors are
"#rrggbb" strings or "default" (the terminal's own color). Missing colors are
taken from the built-in default theme; invalid files fall back to it entirely.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

DEFAULT_THEME = "theme_default.toml"

ROLES = (
    "background",       # main background
    "text",             # primary text
    "text_secondary",   # secondary text: artists, albums, column values
    "muted",            # inactive / hint text
    "accent",           # titles, key hints, active navigation item
    "selection_fg",     # cursor row
    "selection_bg",
    "current_track",    # the playing track wherever it appears
    "progress_fill",
    "progress_empty",
    "border",           # separators
    "error",
    "warning",
)

RGB = tuple[int, int, int]
Color = RGB | None  # None = terminal default

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


class ThemeError(Exception):
    pass


@dataclass(frozen=True)
class Theme:
    name: str
    colors: dict[str, Color]
    source: str


def parse_color(value: object) -> Color:
    if isinstance(value, str):
        if value.strip().lower() == "default":
            return None
        if m := _HEX.match(value.strip()):
            h = m.group(1)
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    raise ThemeError(f"invalid color {value!r} (expected \"#rrggbb\" or \"default\")")


def parse_theme(text: str, source: str, base: Theme | None = None) -> tuple[Theme, list[str]]:
    """Parse theme TOML. Raises ThemeError if the file is unusable."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"{source}: invalid TOML: {exc}") from None
    colors_table = data.get("colors")
    if not isinstance(colors_table, dict):
        raise ThemeError(f"{source}: missing [colors] table")
    warnings = []
    colors: dict[str, Color] = dict(base.colors) if base else {}
    for key, value in colors_table.items():
        if key not in ROLES:
            warnings.append(f"{source}: unknown color '{key}' ignored")
            continue
        try:
            colors[key] = parse_color(value)
        except ThemeError as exc:
            raise ThemeError(f"{source}: {key}: {exc}") from None
    missing = [r for r in ROLES if r not in colors]
    if missing:
        raise ThemeError(f"{source}: missing colors: {', '.join(missing)}")
    name = data.get("name") if isinstance(data.get("name"), str) else Path(source).stem
    return Theme(name=name, colors=colors, source=source), warnings


def builtin_theme_names() -> list[str]:
    return sorted(p.name for p in resources.files("campfire.themes").iterdir()
                  if p.name.endswith(".toml"))


def _read_builtin(name: str) -> str | None:
    path = resources.files("campfire.themes").joinpath(name)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def default_theme() -> Theme:
    theme, _ = parse_theme(_read_builtin(DEFAULT_THEME) or "", f"built-in {DEFAULT_THEME}")
    return theme


def load_theme(name: str, user_dir: Path) -> tuple[Theme, list[str]]:
    """Load a theme by file name (user directory first, then built-in).

    Never raises: problems are reported as warnings and the default is used.
    """
    default = default_theme()
    if not name.endswith(".toml"):
        name += ".toml"
    if "/" in name or "\\" in name or name.startswith("."):
        return default, [f"Theme name {name!r} must be a plain file name; using the default theme."]

    user_path = user_dir / name
    try:
        if user_path.is_file():
            text, source = user_path.read_text(encoding="utf-8"), str(user_path)
        elif (builtin := _read_builtin(name)) is not None:
            text, source = builtin, f"built-in {name}"
        else:
            return default, [f"Theme '{name}' not found; using the default theme."]
    except (OSError, UnicodeDecodeError) as exc:
        return default, [f"Could not read theme '{name}': {exc}; using the default theme."]

    try:
        return parse_theme(text, source, base=default)
    except ThemeError as exc:
        return default, [f"{exc}. Using the default theme."]
