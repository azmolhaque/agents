#!/usr/bin/env python
"""Show what `name_from_local_part` would derive from the real contacts table.

Read-only. Touches no queue, calls no model, writes nothing -- safe to run during a
deploy freeze.

The rule is deliberately strict: only a local part with a separator (`sarah.chen`) is
read as a name, because `jdoe` is an initial and a surname or a nickname or neither,
and "Hi Jdoe" is worse than no greeting in the same way "Hi Support" is. The cost of
that strictness is coverage, and coverage is exactly what nobody can guess -- so this
prints both halves: what would be named, and a sample of what would not.

    .venv/bin/python scripts/preview_contact_names.py

**Read the names before deploying this.** They become the `recipient` line in an
outreach prompt, which is prose a human pastes into an email; every prose defect this
project has shipped was visible in the rendered output and invisible in the code.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from cindraleads.config import settings
from cindraleads.contacts import ROLE_LOCAL_PARTS, name_from_local_part
from cindraleads.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=25, help="rows to print per section")
    args = parser.parse_args()

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    try:
        rows = store.conn.execute(
            "SELECT canonical_domain, email, email_status FROM contacts ORDER BY email"
        ).fetchall()
    finally:
        store.close()

    named: list[tuple[str, str, str]] = []
    unnamed: list[tuple[str, str]] = []
    for row in rows:
        email = str(row["email"])
        name = name_from_local_part(email)
        if name:
            named.append((name, email, str(row["canonical_domain"])))
        else:
            unnamed.append((email, str(row["email_status"])))

    total = len(rows)
    print(f"contacts: {total}   would be named: {len(named)}   left cold: {len(unnamed)}")
    if total:
        print(f"coverage: {100 * len(named) / total:.1f}%\n")

    print(f"--- would be named (showing {min(args.sample, len(named))} of {len(named)})")
    for name, email, domain in named[: args.sample]:
        print(f"  {name:<24} {email:<38} {domain}")

    # The interesting half. A local part with no separator is skipped on purpose, and
    # the shape of what is skipped says whether the strict rule is costing real names
    # or only role mailboxes that were never a person.
    roles = sum(1 for e, _ in unnamed if e.split("@", 1)[0] in ROLE_LOCAL_PARTS)
    print(f"\n--- left cold: {len(unnamed)}, of which {roles} are role mailboxes")
    print(f"    (showing {min(args.sample, len(unnamed) - roles)} that are not)")
    shown = 0
    for email, status in unnamed:
        if email.split("@", 1)[0] in ROLE_LOCAL_PARTS or shown >= args.sample:
            continue
        print(f"  {email:<40} {status}")
        shown += 1

    statuses = Counter(str(r["email_status"]) for r in rows)
    print("\nby status:", ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
