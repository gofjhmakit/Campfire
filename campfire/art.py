"""Album artwork: download cache and decoding through the ffmpeg command-line tool.

The standard library cannot decode JPEG, so images are converted with ffmpeg
(installed alongside mpv on most systems). Without ffmpeg, artwork is simply
not shown.

ffmpeg is run on untrusted data, so the input demuxer is forced from the file's
magic bytes and only the pipe protocol is allowed. This keeps ffmpeg from
probing other formats such as playlists that reference files or URLs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PIXELS = 64          # decoded size for text-mode (half-block) rendering
PNG_SIZE = 480       # size sent to terminals with graphics support
FFMPEG_TIMEOUT = 15

_FORMATS = {b"\xff\xd8\xff": "jpeg_pipe", b"\x89PNG": "png_pipe", b"GIF8": "gif_pipe",
            b"RIFF": "webp_pipe"}


class ArtError(Exception):
    pass


@dataclass(frozen=True)
class Artwork:
    cover_id: str
    rgb: bytes        # PIXELS x PIXELS, 3 bytes per pixel
    png: bytes        # PNG_SIZE x PNG_SIZE

    def downscale(self, width: int, height: int) -> list[list[tuple[int, int, int]]]:
        """Box-filtered width x height pixel grid."""
        out = []
        for y in range(height):
            y0, y1 = y * PIXELS // height, max(y * PIXELS // height + 1, (y + 1) * PIXELS // height)
            row = []
            for x in range(width):
                x0, x1 = x * PIXELS // width, max(x * PIXELS // width + 1, (x + 1) * PIXELS // width)
                r = g = b = n = 0
                for yy in range(y0, y1):
                    base = yy * PIXELS * 3
                    for xx in range(x0, x1):
                        i = base + xx * 3
                        r += self.rgb[i]
                        g += self.rgb[i + 1]
                        b += self.rgb[i + 2]
                        n += 1
                row.append((r // n, g // n, b // n))
            out.append(row)
        return out


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _demuxer(data: bytes) -> str:
    for magic, fmt in _FORMATS.items():
        if data.startswith(magic):
            if fmt == "webp_pipe" and data[8:12] != b"WEBP":
                break
            return fmt
    raise ArtError("unsupported image format")


def _ffmpeg(data: bytes, output_args: list[str]) -> bytes:
    exe = ffmpeg_path()
    if not exe:
        raise ArtError("ffmpeg not found")
    args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe",
            "-f", _demuxer(data), "-i", "pipe:0", "-frames:v", "1", *output_args, "pipe:1"]
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtError(f"ffmpeg failed: {exc}") from None
    if result.returncode != 0 or not result.stdout:
        raise ArtError("ffmpeg could not decode the image")
    return result.stdout


def decode(cover_id: str, data: bytes) -> Artwork:
    # Center-crop to a square, then scale.
    crop = "crop='min(iw,ih)':'min(iw,ih)'"
    rgb = _ffmpeg(data, ["-vf", f"{crop},scale={PIXELS}:{PIXELS}:flags=area",
                         "-f", "rawvideo", "-pix_fmt", "rgb24"])
    if len(rgb) != PIXELS * PIXELS * 3:
        raise ArtError("unexpected decoded image size")
    png = _ffmpeg(data, ["-vf", f"{crop},scale={PNG_SIZE}:{PNG_SIZE}:flags=lanczos",
                         "-f", "image2pipe", "-c:v", "png"])
    return Artwork(cover_id, rgb, png)


class ArtCache:
    """Original image files on disk (private to the user), keyed by cover id."""

    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, cover_id: str) -> Path:
        # Hash the id: it comes from the server and must not become a path.
        return self.dir / (hashlib.sha256(cover_id.encode()).hexdigest()[:32] + ".img")

    def get(self, cover_id: str) -> bytes | None:
        try:
            return self._path(cover_id).read_bytes()
        except OSError:
            return None

    def put(self, cover_id: str, data: bytes) -> None:
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._path(cover_id)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            pass  # the cache is an optimization

    def discard(self, cover_id: str) -> None:
        self._path(cover_id).unlink(missing_ok=True)
