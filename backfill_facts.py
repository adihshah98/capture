"""Write Tier 1 facts.json for sessions that already have a digest.

Reuses the digest each session's extraction left behind, so this is pure
arithmetic — no browser, no replay, no model calls. Sessions whose digest
predates the geometry fields get everything else; depth and dwell stay null
rather than being guessed, which keeps them out of those metrics' denominators.

    python backfill_facts.py            # all pools under data/
    python backfill_facts.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from analyze import ANALYSIS_DIR, BASE_DIR, iter_session_pools, _cli_data_root
from metrics import FACTS_FILE

FACTS_MODULE = BASE_DIR / "src" / "analyze" / "facts.mjs"


def session_dirs(pool: Path) -> list:
    """iter_session_pools yields the sessions/ directory itself, not its parent."""
    if not pool.is_dir():
        return []
    return sorted(d for d in pool.iterdir() if d.is_dir())


def read_digest(session_dir: Path) -> dict | None:
    path = session_dir / ANALYSIS_DIR / "digest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def facts_from_digest(digest: dict) -> dict:
    """Call sessionFacts in facts.mjs — one definition of a fact, not two.

    Reimplementing the shape in Python would let the backfilled rows drift from
    the ones the extractor writes, which is exactly the silent inconsistency
    these metrics are supposed to avoid.
    """
    script = (
        f"import {{ sessionFacts }} from {json.dumps(str(FACTS_MODULE))};"
        "let raw='';process.stdin.on('data',c=>raw+=c);"
        "process.stdin.on('end',()=>"
        "process.stdout.write(JSON.stringify(sessionFacts(JSON.parse(raw)))));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(digest),
        capture_output=True,
        text=True,
        cwd=str(BASE_DIR),
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "").strip().splitlines()[-1:] or "node failed")
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--force", action="store_true", help="rewrite existing facts.json")
    args = ap.parse_args()

    written = skipped = no_digest = failed = 0
    partial = 0

    for pool in iter_session_pools(_cli_data_root()):
        for session_dir in session_dirs(pool):
            out_path = session_dir / ANALYSIS_DIR / FACTS_FILE
            if out_path.is_file() and not args.force:
                skipped += 1
                continue

            digest = read_digest(session_dir)
            if digest is None:
                # Never analysed, so there is nothing to derive facts from.
                no_digest += 1
                continue

            try:
                facts = facts_from_digest(digest)
            except Exception as exc:
                print(f"  failed {session_dir.name}: {exc}", file=sys.stderr)
                failed += 1
                continue

            if facts.get("max_depth_pct") is None:
                partial += 1

            if not args.dry_run:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(facts, indent=2))
            written += 1

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} facts.json")
    if partial:
        print(f"  {partial} without depth/dwell (digest predates geometry fields)")
    if skipped:
        print(f"  {skipped} already had facts (use --force to rewrite)")
    if no_digest:
        print(f"  {no_digest} not yet analysed — nothing to derive from")
    if failed:
        print(f"  {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
