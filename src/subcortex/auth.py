"""The daemon's per-user token.

The daemon listens on 127.0.0.1, which every local user can reach. Requests
must carry the token stored in a 0600 file inside the user's private data dir,
so only this user's own processes can use the daemon (read snapshots, plant
context, spend Jev credits). Web pages are refused before that, by Host,
Origin and Content-Type checks in the daemon.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import data_dir

HEADER = "X-Subcortex-Token"


def token_path() -> Path:
    return data_dir() / "token"


def read_token() -> str:
    try:
        token = token_path().read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    # It goes into a header: anything but hex is a damaged file, not a token.
    return token if token and all(c in "0123456789abcdef" for c in token) else ""


def ensure_token() -> str:
    """The token, created (0600, atomically, first writer wins) if missing."""
    existing = read_token()
    if len(existing) >= 32:
        return existing
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".token-{os.getpid()}-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(secrets.token_hex(32))
    try:
        if path.exists():
            os.replace(tmp, path)  # present but unusable (empty, truncated)
        else:
            os.link(tmp, path)  # fails if another daemon created it meanwhile: use theirs
    except FileExistsError:
        pass
    except OSError:
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return read_token()
