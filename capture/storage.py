"""Where recorded chunks live.

Chunks are write-once, read by name, and deleted wholesale at retention — which
is object storage's shape, not a filesystem's. Keeping them on the instance is
also the last thing tying the app to one box.

Two backends. `local` writes under the data directory exactly as before, so
development needs no AWS. `s3` is production. Both are addressed by the
project's storage_prefix, which is already how the on-disk pools are laid out,
so the same key works in either.

Reads fall back to local disk when a key is missing from S3, so a session
recorded before the migration keeps replaying while the copier works through
the backlog.
"""

import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")
DATA_DIR = DATA_ROOT / "sessions"
PROJECTS_DIR = DATA_ROOT / "projects"

BUCKET = os.environ.get("CAPTURE_S3_BUCKET", "")
S3_PREFIX = os.environ.get("CAPTURE_S3_PREFIX", "sessions").strip("/")
URL_TTL_S = int(os.environ.get("CAPTURE_S3_URL_TTL_S", "900"))
CHUNK_CACHE_CONTROL = "public, max-age=86400, immutable"

_lock = threading.Lock()
_client = None


class Chunk(NamedTuple):
    name: str
    size: int


def configured() -> bool:
    return bool(BUCKET)


def local_dir(storage_prefix: str, session_id: str) -> Path:
    """The on-disk pool, unchanged: "" is the legacy default pool."""
    root = DATA_DIR if not storage_prefix else PROJECTS_DIR / storage_prefix / "sessions"
    return root / session_id


def key_for(storage_prefix: str, session_id: str, name: str) -> str:
    parts = [S3_PREFIX, storage_prefix or "_default", session_id, name]
    return "/".join(p for p in parts if p)


def session_key_prefix(storage_prefix: str, session_id: str) -> str:
    parts = [S3_PREFIX, storage_prefix or "_default", session_id]
    return "/".join(p for p in parts if p) + "/"


def client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import boto3

                _client = boto3.client("s3")
    return _client


def put_chunk(storage_prefix: str, session_id: str, name: str, body: bytes) -> None:
    if configured():
        client().put_object(
            Bucket=BUCKET,
            Key=key_for(storage_prefix, session_id, name),
            Body=body,
            ContentType="application/json",
            # Chunks are immutable once written, so the browser should never
            # re-fetch one. Set on the object because a redirect cannot carry
            # caching for the resource it points at.
            CacheControl=CHUNK_CACHE_CONTROL,
        )
        return
    from .fsutil import atomic_write_bytes

    atomic_write_bytes(local_dir(storage_prefix, session_id) / name, body)


def open_chunk(storage_prefix: str, session_id: str, name: str) -> bytes | None:
    """S3 first, then the local pool. The fallback is what lets a session
    recorded before the migration keep replaying."""
    if configured():
        try:
            return (
                client()
                .get_object(Bucket=BUCKET, Key=key_for(storage_prefix, session_id, name))["Body"]
                .read()
            )
        except Exception:
            pass
    path = local_dir(storage_prefix, session_id) / name
    try:
        return path.read_bytes()
    except OSError:
        return None


def chunk_url(storage_prefix: str, session_id: str, name: str) -> str | None:
    """A presigned URL so the browser fetches from S3 rather than through the
    origin. None means the caller should serve the bytes itself."""
    if not configured():
        return None
    key = key_for(storage_prefix, session_id, name)
    try:
        client().head_object(Bucket=BUCKET, Key=key)
    except Exception:
        return None
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=URL_TTL_S
    )


def list_chunks(storage_prefix: str, session_id: str) -> list:
    """Every chunk for a session, newest last. Merges both backends during the
    migration so a partially-copied session still lists completely."""
    found: dict = {}
    for chunk in _list_local(storage_prefix, session_id):
        found[chunk.name] = chunk
    for chunk in _list_s3(storage_prefix, session_id):
        found[chunk.name] = chunk
    return sorted(found.values(), key=lambda c: (chunk_timestamp(c.name), c.name))


def _list_local(storage_prefix: str, session_id: str) -> Iterable[Chunk]:
    directory = local_dir(storage_prefix, session_id)
    if not directory.is_dir():
        return []
    return [
        Chunk(p.name, p.stat().st_size)
        for p in directory.iterdir()
        if p.is_file() and SAFE_CHUNK.match(p.name)
    ]


def _list_s3(storage_prefix: str, session_id: str) -> Iterable[Chunk]:
    if not configured():
        return []
    out = []
    token = None
    prefix = session_key_prefix(storage_prefix, session_id)
    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client().list_objects_v2(**kwargs)
        for item in page.get("Contents") or []:
            name = item["Key"].rsplit("/", 1)[-1]
            if SAFE_CHUNK.match(name):
                out.append(Chunk(name, item["Size"]))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def delete_session(storage_prefix: str, session_id: str) -> int:
    """Drop a session from both backends. Returns bytes freed on disk."""
    import shutil

    freed = sum(c.size for c in _list_local(storage_prefix, session_id))
    shutil.rmtree(local_dir(storage_prefix, session_id), ignore_errors=True)
    if configured():
        keys = [
            {"Key": key_for(storage_prefix, session_id, c.name)}
            for c in _list_s3(storage_prefix, session_id)
        ]
        for batch in (keys[i : i + 1000] for i in range(0, len(keys), 1000)):
            client().delete_objects(Bucket=BUCKET, Delete={"Objects": batch})
    return freed


def chunk_timestamp(name: str) -> int:
    try:
        return int(name.split("-")[0])
    except ValueError:
        return 0


def chunk_window(name: str) -> str | None:
    parts = name.rsplit(".", 1)[0].split("-")
    return parts[1] if len(parts) == 3 else None
