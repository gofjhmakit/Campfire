"""mpv playback backend, controlled through mpv's JSON IPC socket.

See https://mpv.io/manual/stable/#json-ipc. mpv runs headless (`--idle
--no-video --no-terminal`) as a child process. Stream URLs (which contain an
auth token) are sent over the socket rather than the command line so they are
not visible in the process list.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

# Real level metering: mpv's lavfi bridge runs ffmpeg's astats filter on the
# decoded audio and exposes the measurements as the `af-metadata/viz` property.
VIZ_FILTER = "@viz:lavfi=[astats=metadata=1:reset=1:measure_overall=none]"

# The stream URLs carry Subsonic auth parameters (u/t/s), which can be replayed
# against the API. These options are always forced on mpv's command line (which
# overrides mpv.conf) so the URLs are only ever sent over verified TLS and are
# never written to disk or handed to another program:
#   tls-verify       older FFmpeg builds do not verify certificates by default
#   ytdl=no          the ytdl hook would run `yt-dlp <url>`, exposing it in `ps`
#   log-file=        a log file configured in mpv.conf would record the URLs
#   save-position-on-quit=no   watch-later files can record the URLs
SAFETY_OPTIONS = ("--tls-verify=yes", "--ytdl=no", "--log-file=", "--save-position-on-quit=no")

OBSERVED = ("pause", "time-pos", "duration", "idle-active", "seekable",
            "paused-for-cache", "volume")


class BackendError(Exception):
    pass


class MpvBackend:
    """Owns the mpv process. Events are delivered as ("mpv", dict) on `events`."""

    def __init__(self, events: queue.Queue, socket_dir: Path, mpv_path: str = "mpv",
                 volume: int = 80, visualizer: bool = True, user_config: bool = False):
        self.events = events
        self.socket_dir = socket_dir
        self.mpv_path = mpv_path
        self.initial_volume = volume
        self.visualizer = visualizer
        # mpv.conf and user scripts can read the current URL (and thus the auth
        # token), e.g. presence or scrobbling scripts, so they are off by default.
        self.user_config = user_config
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._ids = itertools.count(1)
        # request_id -> (event to wake a waiting request(), or None for fire-and-forget; reply box)
        self._pending: dict[int, tuple[threading.Event | None, list]] = {}
        self._socket_path: Path | None = None
        self._closing = False

    # -- Lifecycle ---------------------------------------------------------------

    @staticmethod
    def find(mpv_path: str) -> str | None:
        return shutil.which(mpv_path)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._sock is not None

    def start(self) -> None:
        if self.running:
            return
        self.shutdown()
        exe = self.find(self.mpv_path)
        if not exe:
            raise BackendError(f"mpv not found ('{self.mpv_path}'). Install it, e.g. `brew install mpv` "
                               "or `sudo apt install mpv`.")
        self.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.socket_dir, 0o700)  # only this user may talk to the mpv socket
        path = self.socket_dir / f"mpv-{os.getpid()}.sock"
        path.unlink(missing_ok=True)
        self._closing = False
        self._proc = subprocess.Popen(self.command_line(exe, path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self._socket_path = path
        self._sock = self._connect(path)
        threading.Thread(target=self._reader, name="mpv-ipc", daemon=True).start()
        for n, name in enumerate(OBSERVED, start=1):
            self._send(["observe_property", n, name])

    def command_line(self, exe: str, socket_path: Path) -> list[str]:
        args = [exe]
        if not self.user_config:
            args.append("--no-config")
        args += [*SAFETY_OPTIONS, "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
                 "--audio-display=no", "--input-default-bindings=no", "--keep-open=no",
                 "--gapless-audio=no", f"--volume={self.initial_volume}",
                 f"--input-ipc-server={socket_path}"]
        if self.visualizer:
            args.append(f"--af={VIZ_FILTER}")
        return args

    def _connect(self, path: Path) -> socket.socket:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                code = self._proc.returncode
                self._proc = None
                raise BackendError(f"mpv exited during startup (code {code}).")
            if path.exists():
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(str(path))
                    return sock
                except OSError:
                    sock.close()
            time.sleep(0.05)
        self.shutdown()
        raise BackendError("Timed out connecting to mpv's IPC socket.")

    def shutdown(self) -> None:
        self._closing = True
        if self._sock:
            try:
                self._send(["quit"])
            except BackendError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        if self._socket_path:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path = None
        for done, _ in list(self._pending.values()):
            if done:
                done.set()
        self._pending.clear()

    # -- IPC ---------------------------------------------------------------------

    def _send(self, command: list, request_id: int | None = None) -> None:
        if not self._sock:
            raise BackendError("mpv is not running.")
        message = {"command": command}
        if request_id is not None:
            message["request_id"] = request_id
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except OSError as exc:
                raise BackendError(f"Lost connection to mpv ({exc.strerror or exc}).") from None

    def request(self, command: list, timeout: float = 1.0) -> object:
        """Send a command and wait for its reply. Raises BackendError on failure."""
        request_id = next(self._ids)
        done, box = threading.Event(), []
        self._pending[request_id] = (done, box)
        try:
            self._send(command, request_id)
            if not done.wait(timeout) or not box:
                raise BackendError(f"mpv did not answer '{command[0]}'.")
        finally:
            self._pending.pop(request_id, None)
        reply = box[0]
        if reply.get("error") != "success":
            raise BackendError(f"mpv: {reply.get('error')}")
        return reply.get("data")

    def command(self, *args: object) -> None:
        """Fire-and-forget command; failures arrive as a 'command-error' event."""
        request_id = next(self._ids)
        self._pending[request_id] = (None, [args[0]])
        self._send(list(args), request_id)

    def _reader(self) -> None:
        sock, buf = self._sock, b""
        try:
            while sock:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(line)
        except OSError:
            pass
        if not self._closing:
            self.events.put(("mpv", {"event": "backend-exited"}))

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if "request_id" in msg and "event" not in msg:
            entry = self._pending.pop(msg["request_id"], None)
            if entry is None:
                return
            done, box = entry
            if done is None:  # fire-and-forget: report errors only
                if msg.get("error") != "success":
                    self.events.put(("mpv", {"event": "command-error", "command": box[0],
                                             "error": msg.get("error")}))
                return
            box.append(msg)
            done.set()
        elif "event" in msg:
            self.events.put(("mpv", msg))

    # -- Playback commands -----------------------------------------------------------

    def load(self, url: str) -> None:
        self.command("set_property", "pause", False)
        self.command("loadfile", url, "replace")

    def set_pause(self, paused: bool) -> None:
        self.command("set_property", "pause", paused)

    def seek(self, seconds: float, absolute: bool = False) -> None:
        self.command("seek", seconds, "absolute" if absolute else "relative")

    def stop(self) -> None:
        self.command("stop")

    def set_volume(self, volume: int) -> None:
        self.command("set_property", "volume", volume)

    def levels(self) -> list[float] | None:
        """Current per-channel RMS levels in dBFS, or None if unavailable."""
        if not self.visualizer or not self.running:
            return None
        try:
            data = self.request(["get_property", "af-metadata/viz"], timeout=0.2)
        except BackendError:
            return None
        if not isinstance(data, dict):
            return None
        levels = []
        for channel in (1, 2):
            value = data.get(f"lavfi.astats.{channel}.RMS_level")
            if value is None:
                break
            try:
                levels.append(float(value))
            except ValueError:
                levels.append(float("-inf"))
        return levels or None
