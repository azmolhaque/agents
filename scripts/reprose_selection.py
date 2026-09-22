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

from cindraleads.agents.scorer import (
    _SPENT,
    DEFAULT_RESCORE_LIMIT,
    score_dedupe_key,
    stale_selection,
)
from cindraleads.config import settings
from cindraleads.store import Store

#: The statuses the command itself treats as spent, borrowed rather than restated --
#: a report that split the rows differently from the code would explain a pass nobody
#: runs.
SPENT = _SPENT
NEW = "would be queued"


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
            held.append((domain, str(job["status"]) if job else NEW))
    finally:
        store.close()

    if not rows:
        print("the reprose selection is empty")
        return 1

    print(f"{len(rows)} row(s) in the next `--reprose` pass\n")
    for state, count in Counter(state for _domain, state in held).most_common():
        print(f"  {count:>5}  {state}")

    spent = sum(1 for _domain, state in held if state in SPENT)
    waiting = sum(1 for _domain, state in held if state not in SPENT and state != NEW)
    if spent:
        print(
            f"\n  {spent} of {len(rows)} are held by a job that already finished and wrote\n"
            "  no angle. `--reprose` re-queues those under a fresh key: the row being in\n"
            "  this selection *is* the evidence that the finished job achieved nothing,\n"
            "  which is the one thing `--force` normally has to be told by a human.\n"
            "  They are real work, so expect `queued` to be about this number."
        )
    if waiting:
        print(
            f"\n  {waiting} are held by a job still pending or in flight. Those are not\n"
            "  re-queued -- that is the other reading of a low `queued`, and it means\n"
            "  wait rather than run it again."
        )
    if not spent and not waiting:
        print("\n  nothing is held; every row in this selection is new work")

    print(f"\n{'':<4} {'domain':<34} holder")
    for domain, state in held[: args.examples]:
        print(f"  {domain:<36} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
