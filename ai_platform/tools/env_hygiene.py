"""
Lightweight secrets-hygiene check for the `.env` file both entry points
(`chat.py`, `server.py`) load.

`.env` holds the LLM provider's API key in plaintext (see `.env.example`) —
fine for a local-dev tool, as long as the file itself isn't readable by
every other user on the machine. This doesn't add a vault or rotate
anything; it just makes the risk visible instead of silent, the same way
the existing "LLM_PROVIDER is set but the key is missing" check does.
"""

import os
import stat
from typing import Optional

from dotenv import find_dotenv


def check_env_file_permissions() -> Optional[str]:
    """
    If a `.env` file is in use and readable/writable by the file's group or
    by other users on the machine, return a warning string describing the
    fix. Returns `None` if there's nothing to warn about (no `.env` file
    found — e.g. real environment variables are used instead — or its
    permissions are already owner-only).
    """
    path = find_dotenv(usecwd=True)
    if not path or not os.path.exists(path):
        return None

    mode = stat.S_IMODE(os.stat(path).st_mode)
    # Anything beyond owner read/write (0o600) means group or other can read
    # a file holding a real API key in plaintext.
    if mode & 0o077:
        return (
            f"!! {path} is readable by other users on this machine "
            f"(mode {oct(mode)}). It holds your LLM API key in plaintext — "
            f"run `chmod 600 {path}` to restrict it to your own user."
        )
    return None
