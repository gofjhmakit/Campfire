"""Storage for the Bandcamp Subsonic credentials.

Order of preference:
1. Environment variables CAMPFIRE_SUBSONIC_USER / CAMPFIRE_SUBSONIC_PASSWORD
   (read-only; useful for scripted integration tests).
2. The system keyring: macOS Keychain via `security`, or the Secret Service
   via `secret-tool` (libsecret) on Linux.
3. A credentials.toml file in the config directory with 0600 permissions.

The password never appears on a command line: it is passed on stdin.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir

SERVICE = "campfire-bandcamp-subsonic"
ENV_USER = "CAMPFIRE_SUBSONIC_USER"
ENV_PASSWORD = "CAMPFIRE_SUBSONIC_PASSWORD"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    source: str = ""

    def __repr__(self) -> str:  # never leak the password through repr/logging
        return f"Credentials(username={self.username!r}, password='***', source={self.source!r})"


def clean(value: str) -> str:
    # Bandcamp's settings page has been seen adding a leading space when copying.
    return value.strip()


def credentials_file() -> Path:
    return config_dir() / "credentials.toml"


def backend_name() -> str:
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if shutil.which("secret-tool"):
        return "secret-service"
    return "file"


def load() -> Credentials | None:
    user, password = os.environ.get(ENV_USER), os.environ.get(ENV_PASSWORD)
    if user and password:
        return Credentials(clean(user), clean(password), "environment")

    backend = backend_name()
    if backend == "keychain":
        creds = _keychain_load()
    elif backend == "secret-service":
        creds = _secret_tool_load()
    else:
        creds = None
    return creds or _file_load()


def save(username: str, password: str) -> str:
    """Store credentials; returns a description of where they were stored."""
    username, password = clean(username), clean(password)
    if not username or not password:
        raise CredentialError("Username and password must not be empty.")
    if "\n" in username or "\n" in password:
        raise CredentialError("Credentials must not contain newlines.")

    backend = backend_name()
    if backend == "keychain":
        _keychain_save(username, password)
        return "macOS Keychain"
    if backend == "secret-service":
        try:
            _secret_tool_save(username, password)
            return "Secret Service keyring"
        except CredentialError:
            pass  # e.g. no keyring daemon running; fall back to the file
    _file_save(username, password)
    return str(credentials_file())


def delete() -> None:
    backend = backend_name()
    if backend == "keychain":
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE],
                       capture_output=True)
    elif backend == "secret-service":
        subprocess.run(["secret-tool", "clear", "service", SERVICE], capture_output=True)
    credentials_file().unlink(missing_ok=True)


# -- macOS Keychain ---------------------------------------------------------

def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_save(username: str, password: str) -> None:
    # `security -i` reads commands from stdin, keeping the password out of argv.
    command = (f"add-generic-password -U -s {_quote(SERVICE)} -a {_quote(username)} "
               f"-l {_quote('Campfire Bandcamp Subsonic')} -w {_quote(password)}\n")
    result = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip() or result.stderr.strip():
        raise CredentialError("Could not store credentials in the Keychain.")


def _keychain_load() -> Credentials | None:
    pw = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-w"],
                        capture_output=True, text=True)
    if pw.returncode != 0:
        return None
    info = subprocess.run(["security", "find-generic-password", "-s", SERVICE],
                          capture_output=True, text=True)
    match = re.search(r'"acct"<blob>="(.*)"', info.stdout)
    if not match:
        return None
    return Credentials(match.group(1), pw.stdout.rstrip("\n"), "macOS Keychain")


# -- Linux Secret Service ----------------------------------------------------

def _secret_tool_save(username: str, password: str) -> None:
    subprocess.run(["secret-tool", "clear", "service", SERVICE], capture_output=True)
    result = subprocess.run(
        ["secret-tool", "store", "--label=Campfire Bandcamp Subsonic",
         "service", SERVICE, "username", username],
        input=password, capture_output=True, text=True)
    if result.returncode != 0:
        raise CredentialError("Could not store credentials with secret-tool.")


def _secret_tool_load() -> Credentials | None:
    result = subprocess.run(["secret-tool", "search", "service", SERVICE],
                            capture_output=True, text=True)
    match = re.search(r"^attribute\.username = (.*)$", result.stdout, re.MULTILINE)
    if result.returncode != 0 or not match:
        return None
    pw = subprocess.run(["secret-tool", "lookup", "service", SERVICE,
                         "username", match.group(1)], capture_output=True, text=True)
    if pw.returncode != 0 or not pw.stdout:
        return None
    return Credentials(match.group(1), pw.stdout.rstrip("\n"), "Secret Service keyring")


# -- Plain file fallback -------------------------------------------------------

def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _file_save(username: str, password: str) -> None:
    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"username = {_toml_str(username)}\npassword = {_toml_str(password)}\n")
    os.chmod(path, 0o600)


def _file_load() -> Credentials | None:
    path = credentials_file()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    user, password = data.get("username"), data.get("password")
    if not isinstance(user, str) or not isinstance(password, str):
        return None
    return Credentials(clean(user), clean(password), str(path))
