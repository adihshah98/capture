"""Where data lives, and what a valid id looks like.

One definition each. These constants were copied into server.py, projects.py and
storage.py, and the copies drifted: storage.py resolved BASE_DIR relative to its
own file, which was the repo root until the modules moved under capture/ and
silently became capture/ itself. Its local-disk fallback then pointed at
capture/data while everything else used <repo>/data, so a session whose chunks
were only on disk stopped being readable. Every test monkeypatches DATA_DIR, so
nothing caught it.

Read these through the module — `paths.DATA_DIR`, not `from .paths import
DATA_DIR` — so redirecting the data root is one assignment rather than one per
importer.
"""

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")
DATA_DIR = DATA_ROOT / "sessions"
PROJECTS_DIR = DATA_ROOT / "projects"

# Written by the pre-Postgres layout; backfill.py still imports them to migrate.
PROJECTS_PATH = DATA_ROOT / "projects.json"
CONFIG_PATH = DATA_ROOT / "config.json"

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")


def pool_for(storage_prefix: str) -> Path:
    """The directory holding a project's sessions. "" is the legacy default pool."""
    if not storage_prefix:
        return DATA_DIR
    return PROJECTS_DIR / storage_prefix / "sessions"


def chunk_timestamp(name: str) -> int:
    try:
        return int(name.split("-")[0])
    except ValueError:
        return 0


def chunk_window(name: str) -> str | None:
    parts = name.rsplit(".", 1)[0].split("-")
    return parts[1] if len(parts) == 3 else None
