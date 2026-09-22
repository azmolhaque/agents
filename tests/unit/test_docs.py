"""CLAUDE.md is the first thing anyone reads to orient, and nothing checked it.

Two blocks in it are not prose but a claim about the repository -- the file map under
`## Where things are` and the command list under `## Commands`. Both had drifted, and
both drifted in the quiet direction: the map named two directories that do not exist,
one of them backing a testing rule ("re-run the golden fixtures") that therefore could
not be followed, and was not, when `outreach_angle.md` changed on 2026-09-22.

The tests here read those blocks rather than restating them, the same reason
`test_systemd_waits_longer_than_a_stage_may_run` reads the unit file rather than
repeating its number: a guard that carries its own copy of the fact is a second place
for the fact to be wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO / "CLAUDE.md"

# A path-shaped token: it contains a slash and nothing that is not legal in a path or a
# glob. Prose in these blocks ("edit these, not code", "unit, integration, chaos") has
# no slash in it, so the slash is the discriminator and a comma or a space is the
# escape hatch for anything that must not be read as a path.
_PATHISH = re.compile(r"^[\w.*/-]+/[\w.*/-]*$")


def _section(heading: str) -> str:
    """The text under a `## heading`, up to the next one."""
    text = CLAUDE_MD.read_text()
    start = text.index(f"\n## {heading}\n") + 1
    rest = text[start + 1 :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _fenced(section: str) -> str:
    blocks = re.findall(r"^```\n(.*?)^```", section, re.MULTILINE | re.DOTALL)
    assert blocks, "the section must still carry a fenced block"
    return blocks[0]


def test_every_path_in_the_file_map_exists() -> None:
    """The map pointed at `tests/golden/` and `mcp_servers/` for the life of the
    project and neither has ever existed.

    Both directions are asserted, because the note recording what is *not* built is as
    capable of going stale as the map itself: the day somebody writes `mcp_servers/`,
    that paragraph becomes the wrong thing to read and this test is what says so.
    """
    section = _section("Where things are")

    documented = sorted({token for token in _fenced(section).split() if _PATHISH.match(token)})
    assert len(documented) >= 6, f"the map lost most of its entries: {documented}"

    missing = []
    for token in documented:
        if "*" in token:
            if not list(REPO.glob(token)):
                missing.append(token)
        elif not (REPO / token).exists():
            missing.append(token)
    assert not missing, (
        f"the file map points at paths that do not exist: {missing}. The map is the "
        "first thing anyone reads to orient; a wrong entry sends them looking for a "
        "directory, or -- as with tests/golden/ -- makes a testing rule unfollowable."
    )

    claimed_absent = re.search(
        r"\*\*Not built, and listed here because this map used to claim they "
        r"were:\*\*(.+?)(?:\n\n|\Z)",
        section,
        re.DOTALL,
    )
    assert claimed_absent, "the note recording what is not built must stay in the map"
    for token in re.findall(r"`([^`]+)`", claimed_absent.group(1)):
        if not _PATHISH.match(token):
            continue  # a test name, not a path
        assert not (REPO / token).exists(), (
            f"{token} exists now, so the paragraph saying it does not is the stale "
            "half. Move it into the map above."
        )


def test_every_command_is_documented_and_every_documented_command_exists() -> None:
    """The command block is a list somebody consults instead of `--help`, so an entry
    that does not exist wastes a run and an entry that is missing hides a command.

    The reverse half is the one that was failing. `cindra suppress` and `cindra
    worklist` are named repeatedly in the prose of this same file -- the suppression
    list is half of how a publisher stops reaching a card, and the worklist is the
    call list a human actually works -- while the block anyone reads for the list of
    commands had neither.

    No exemption list, deliberately. An exemption list is a second place to register a
    command and therefore a second place to forget one, which is the argument
    `auth_tokens_for` already settled by resolving tokens by convention.
    """
    import typer.main as typer_main

    from cindraleads.cli import app

    groups: dict[str, set[str]] = {}
    registered: set[str] = set()

    def name_of(command: object) -> str:
        explicit = getattr(command, "name", None)
        if explicit:
            return str(explicit)
        return str(typer_main.get_command_name(command.callback.__name__))  # type: ignore[attr-defined]

    for command in app.registered_commands:
        registered.add(name_of(command))
    for group in app.registered_groups:
        group_name = str(group.name)
        subs = {name_of(c) for c in group.typer_instance.registered_commands}
        groups[group_name] = subs
        registered.update(f"{group_name} {sub}" for sub in subs)

    documented: set[str] = set()
    for raw in _fenced(_section("Commands")).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("cindra "):
            continue
        # Split on a spaced pipe only: `good|bad` is one argument's alternatives,
        # `db status | db backup` is two commands on one line.
        for segment in line[len("cindra ") :].split(" | "):
            words = [w for w in segment.split() if w[:1].isalpha()]
            if not words:
                continue
            if words[0] in groups and len(words) > 1:
                documented.add(f"{words[0]} {words[1]}")
            else:
                documented.add(words[0])

    assert not (documented - registered), (
        f"CLAUDE.md documents commands the CLI does not have: {sorted(documented - registered)}"
    )
    assert not (registered - documented), (
        f"the CLI has commands CLAUDE.md does not list: "
        f"{sorted(registered - documented)}. The block is where anyone looks for the "
        "list, so a command missing from it is one nobody runs."
    )


# --------------------------------------------------- the defect that recurs most

# Overrides a framework calls by name. Nothing in this repository references them and
# nothing should: the stdlib's HTTP server and HTML parser, and Pydantic, do the
# calling. Permanently exempt.
_CALLED_BY_A_FRAMEWORK = frozenset(
    {
        "do_GET",
        "log_message",
        "handle_starttag",
        "handle_endtag",
        "handle_data",
        "model_post_init",
    }
)

# Built, exported, and connected to nothing -- the standing inventory of the defect
# this project has now paid for ten times: `digest_pages`, `extend_lease`,
# `open_roles`, `discovered_by`, `full_name`, the heartbeat `exiting` flag, `_facts`,
# `GITHUB_TOKEN`, the `enqueue_stale_scores` limit, and `digest_summary`.
#
# Every entry here is a decision someone owes: wire it or delete it. The list exists so
# the eleventh instance is visible on the day it is written rather than found months
# later on a card that reached a prospect.
_KNOWN_UNCONNECTED: dict[str, str] = {
    "by_role": "SourceRegistry accessor; `free_sources` and `by_class` are used",
    "enqueue_many": "a loop over `enqueue`; every caller writes the loop itself",
    "known_codes": "`set(cfg.triggers)`, which every caller writes inline",
    "notify_status": "sd_notify STATUS=; the worker sends READY and WATCHDOG only",
    "rapidfuzz_available": "`name_similarity` degrades on its own; nothing asks",
    "score_stamp": "`to_iso(when)` under another name",
    "templates_for": "`coverage()` answers the same question and is used",
}


def _never_referenced() -> set[str]:
    """Functions defined in `src/cindraleads` and named nowhere in the repository.

    Decorated definitions are excluded: a Typer command, a Pydantic validator and a
    discord.py event handler are all registered by their decorator and referenced by
    nothing, which is correct and would otherwise bury the signal.
    """
    import ast
    from collections import Counter

    defined: dict[str, str] = {}
    decorated: set[str] = set()
    for path in (REPO / "src" / "cindraleads").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.setdefault(node.name, str(path))
                if node.decorator_list:
                    decorated.add(node.name)

    used: Counter[str] = Counter()
    for root in ("src", "tests", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Name):
                    used[node.id] += 1
                elif isinstance(node, ast.Attribute):
                    used[node.attr] += 1

    return {
        name
        for name in defined
        if not used[name] and not name.startswith("__") and name not in decorated
    }


def test_nothing_new_is_built_and_connected_to_nothing() -> None:
    """The defect this project keeps paying for, asked mechanically.

    Ten instances so far, and every one was found by a card reaching a prospect, a
    column that was NULL for months, or a report reading `(unknown) 201`.
    `digest_summary` was the tenth and it was found by exactly this scan -- written
    with its rationale in its own docstring, sitting directly beneath `digest_pages`,
    which was the first.

    A new orphan is not necessarily a bug. It is a question that has to be answered
    the day it appears, while the author still knows the answer.
    """
    unexplained = _never_referenced() - _CALLED_BY_A_FRAMEWORK - set(_KNOWN_UNCONNECTED)
    assert not unexplained, (
        f"defined and referenced nowhere: {sorted(unexplained)}. Wire it, delete it, "
        "or add it to _KNOWN_UNCONNECTED with the reason -- built-wired-never-connected "
        "is the defect this project has shipped ten times."
    )


def test_the_unconnected_list_does_not_outlive_what_it_records() -> None:
    """The quieter half, and the one that rots first.

    A name that gets wired up or deleted has to leave the list, or the list stops
    describing the repository and becomes another thing that claims something untrue
    about it -- which is the file map defect above, one directory over.
    """
    stale = set(_KNOWN_UNCONNECTED) - _never_referenced()
    assert not stale, (
        f"{sorted(stale)} are referenced now (or gone), so _KNOWN_UNCONNECTED is "
        "describing a repository that no longer exists"
    )
