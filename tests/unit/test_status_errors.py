"""`cindra status`'s error list, which has to tell a live fault from a healed one."""

from __future__ import annotations

from cindraleads.cli import error_line


def _row(**over: object) -> dict[str, object]:
    base = {
        "kind": "enrich.company",
        "status": "done",
        "updated_at": "2026-08-23T02:20:10.709Z",
        "last_error": "CindraError: stage enrich.company exceeded 900s and was cancelled",
    }
    base.update(over)
    return base


def test_a_job_that_recovered_does_not_read_as_a_live_fault() -> None:
    """Two of these sat at the top of `recent errors` for hours. Both had completed on
    the next attempt -- one of them three weeks earlier -- and a round of diagnosis went
    into asking whether a bound shipped that morning was holding."""
    line = error_line(_row())

    assert "recovered" in line
    assert "2026-08-23 02:20" in line, "the date is half of what makes it answerable"


def test_a_job_still_carrying_the_error_says_so() -> None:
    line = error_line(_row(status="pending"))

    assert "recovered" not in line
    assert "pending" in line
