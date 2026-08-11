"""Copy chunks already on disk into the bucket.

Runs alongside live traffic. Nothing depends on it finishing: reads fall back
to the local pool, so a session that has not been copied yet still replays.

Idempotent and resumable — it lists what a session already has in the bucket
and uploads only the difference, so re-running after an interruption picks up
where it stopped. One LIST per session rather than a HEAD per chunk: at
production sizes that is tens of thousands of calls instead of hundreds of
thousands.

Local files are never deleted. Retention removes them on its own schedule, and
keeping them is what makes the switch reversible.
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import projects
import storage

DEFAULT_CONCURRENCY = int(os.environ.get("CAPTURE_S3_COPY_CONCURRENCY", "8"))


class Progress:
    def __init__(self, total_sessions: int):
        self.total_sessions = total_sessions
        self.sessions = 0
        self.uploaded = 0
        self.skipped = 0
        self.failed = 0
        self.bytes = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def chunk(self, *, uploaded=0, skipped=0, failed=0, size=0):
        with self._lock:
            self.uploaded += uploaded
            self.skipped += skipped
            self.failed += failed
            self.bytes += size

    def session_done(self, verbose: bool):
        with self._lock:
            self.sessions += 1
            done, total = self.sessions, self.total_sessions
        if verbose and (done % 250 == 0 or done == total):
            print(f"  {self.line(done, total)}", flush=True)

    def line(self, done: int, total: int) -> str:
        elapsed = max(time.monotonic() - self.started, 0.001)
        rate = done / elapsed
        remaining = (total - done) / rate if rate else 0
        return (
            f"{done}/{total} sessions | {self.uploaded} up, {self.skipped} already there"
            f"{f', {self.failed} failed' if self.failed else ''}"
            f" | {self.bytes / 1024**3:.2f} GB | {rate:.1f}/s | eta {remaining / 60:.0f}m"
        )


def local_chunks(session_dir: Path) -> list:
    if not session_dir.is_dir():
        return []
    return [
        p for p in session_dir.iterdir()
        if p.is_file() and storage.SAFE_CHUNK.match(p.name)
    ]


def copy_session(prefix: str, session_dir: Path, progress: Progress, dry_run: bool) -> None:
    session_id = session_dir.name
    local = local_chunks(session_dir)
    if not local:
        return
    already = {c.name for c in storage._list_s3(prefix, session_id)}
    for path in local:
        if path.name in already:
            progress.chunk(skipped=1)
            continue
        if dry_run:
            progress.chunk(uploaded=1, size=path.stat().st_size)
            continue
        try:
            body = path.read_bytes()
            storage.put_chunk(prefix, session_id, path.name, body)
            progress.chunk(uploaded=1, size=len(body))
        except Exception as exc:
            progress.chunk(failed=1)
            print(f"  failed {session_id}/{path.name}: {exc}", file=sys.stderr, flush=True)


def copy_all(concurrency: int = DEFAULT_CONCURRENCY, limit=None,
             dry_run: bool = False, verbose: bool = True) -> Progress:
    work = []
    for project in projects.all_projects(fresh=True):
        pool = project.pool
        if not pool.is_dir():
            continue
        for entry in sorted(pool.iterdir()):
            if entry.is_dir():
                work.append((project.storage_prefix, entry))
    if limit:
        work = work[:limit]

    progress = Progress(len(work))
    if verbose:
        print(f"copying {len(work)} session(s) with {concurrency} workers"
              f"{' (dry run)' if dry_run else ''}", flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as pool_exec:
        futures = [
            pool_exec.submit(copy_session, prefix, entry, progress, dry_run)
            for prefix, entry in work
        ]
        for future in futures:
            future.result()
            progress.session_done(verbose)
    return progress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, help="only the first N sessions")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not storage.configured():
        print("CAPTURE_S3_BUCKET is not set — nothing to copy to", file=sys.stderr)
        return 1

    started = time.monotonic()
    progress = copy_all(args.concurrency, args.limit, args.dry_run)
    print(
        f"done in {(time.monotonic() - started) / 60:.1f}m: "
        f"{progress.uploaded} uploaded, {progress.skipped} already present, "
        f"{progress.failed} failed, {progress.bytes / 1024**3:.2f} GB"
    )
    return 1 if progress.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import db

        db.close_pool()
