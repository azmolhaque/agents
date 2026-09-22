#!/usr/bin/env python3
"""What would `cindra reconcile --reprose` queue, and what is already spent?

`queued 2` out of a 50-row selection has two readings and the command cannot tell them
apart. Either 48 jobs are still sitting in the queue from the last pass -- fine, wait --
or 48 dedupe keys belong to jobs that **already ran and produced no angle**, in which
case the rows sort to the front of every future pass and can never be queued again.
`enqueue` matches `dedupe_key` across completed jobs too, which is exactly what makes a
rescore idempotent and exactly what makes that second case permanent.

It is not hypothetical here: 54 thermal pauses in one day is 54 score jobs that
completed successfully having written nothing, and `--force` is the documented escape
for "a job ran but achieved nothing", which no query over the leads can detect.

So: the real selection, the real key, and the state of whatever holds it. Reads the
database and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from cindraleads.agents.scorer import DEFAULT_RESCORE_LIMIT, score_dedupe_key, stale_selection
from cindraleads.config import settings
from cindraleads.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_RESCORE_LIMIT)
    parser.add_argument("--examples", type=int, default=10, help="rows to print")
    args = parser.parse_args()

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    try:
        rows, fingerprint, prose_ver = stale_selection(store, reprose=True, limit=args.limit)
        held: list[tuple[str, str]] = []
        for row in rows:
            domain = str(row["domain"])
            key = score_dedupe_key(domain, row["newest"], fingerprint, prose_ver)
            job = store.conn.execute(
                "SELECT status, attempts, created_at FROM jobs WHERE dedupe_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            held.append((domain, str(job["status"]) if job else "would be queued"))
    finally:
        store.close()

    if not rows:
        print("the reprose selection is empty")
        return 1

    print(f"{len(rows)} row(s) in the next `--reprose` pass\n")
    for state, count in Counter(state for _domain, state in held).most_common():
        print(f"  {count:>5}  {state}")

    spent = sum(1 for _domain, state in held if state == "done")
    if spent:
        print(
            f"\n  {spent} of {len(rows)} are held by a job that already ran to completion.\n"
            "  Those rows keep their angle, so they sort to the front of every pass and\n"
            "  this command will queue none of them again. `cindra reconcile --force\n"
            "  --reprose` is the escape -- it adds a nonce to the key -- and it also\n"
            "  re-queues enrichment, so read that cost before reaching for it."
        )
    else:
        print("\n  nothing is held by a completed job; a low `queued` means the queue is busy")

    print(f"\n{'':<4} {'domain':<34} holder")
    for domain, state in held[: args.examples]:
        print(f"  {domain:<36} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
