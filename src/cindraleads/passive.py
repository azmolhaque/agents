"""The passive-only boundary, as code that refuses.

Cindrasec's public promise is *"no scan ever starts without a signed Rules of
Engagement."* A prospecting bot that probed prospects would destroy that promise and
create real legal exposure, so the rule cannot live in a comment or a code review
convention. It lives here, and it raises.

Three layers, and only the first two are in this file:

1. **Legality class at the source registry.** A source with no declared class cannot be
   fetched at all — `sources.yaml` refuses to load it. That is the layer that actually
   constrains the pipeline, because the pipeline has exactly one egress function.
2. **A binary denylist.** Nothing here shells out to a scanner today. The denylist
   exists so that the day someone adds a `subprocess` call, it fails loudly rather than
   working. A guard that only matters in the future is still worth writing when the
   thing it guards against is a legal boundary.
3. Dev-time: a PreToolUse hook blocking the same commands in Claude Code.

The forbidden list is *not* about what is technically hard. Every one of these is
trivial to run. It is about what would make us the kind of company that scans people
who never asked.
"""

from __future__ import annotations

import shlex

from cindraleads.errors import CindraError

__all__ = [
    "FORBIDDEN_BINARIES",
    "PassiveOnlyViolation",
    "assert_command_allowed",
    "is_forbidden_command",
]


class PassiveOnlyViolation(CindraError):
    """An action that would touch a prospect's infrastructure was attempted."""


# Port and service scanners, vulnerability scanners, brute-forcers, and credential
# tooling. Kept as bare binary names because that is what a `subprocess` call would
# actually contain.
FORBIDDEN_BINARIES: frozenset[str] = frozenset(
    {
        # port / service / version detection
        "nmap",
        "masscan",
        "naabu",
        "rustscan",
        "zmap",
        "unicornscan",
        "hping3",
        # vulnerability scanning
        "nuclei",
        "nikto",
        "zap",
        "zap.sh",
        "zap-cli",
        "sqlmap",
        "wpscan",
        "openvas",
        "nessus",
        "arachni",
        "wapiti",
        "skipfish",
        "vega",
        # content discovery / brute force
        "gobuster",
        "dirb",
        "dirbuster",
        "ffuf",
        "feroxbuster",
        "wfuzz",
        "dirsearch",
        "sublist3r",
        "dnsrecon",
        "fierce",
        "knockpy",
        "amass",
        # credential attacks
        "hydra",
        "medusa",
        "patator",
        "ncrack",
        "crackmapexec",
        "netexec",
        "john",
        "hashcat",
        "responder",
        # exploitation / post-exploitation
        "metasploit",
        "msfconsole",
        "msfvenom",
        "sqlninja",
        "beef",
        "empire",
        "cobaltstrike",
        "sliver",
        # raw traffic tools pointed at a third party
        "tcpdump",
        "ettercap",
        "bettercap",
        "mitmproxy",
        "mitmdump",
    }
)

# Substrings that betray an active probe even when the binary itself is innocent:
# `curl` is fine, `curl --user admin:admin` against a prospect is not.
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "vrfy ",
    "rcpt to",
    "--user-agent-random",
)


def is_forbidden_command(command: str | list[str]) -> str | None:
    """The reason this command is forbidden, or None.

    Matches on the binary's basename so `/usr/bin/nmap` and `./nmap` are both caught,
    and on any argument that is itself a forbidden binary — `sh -c "nmap ..."` and
    `xargs nmap` do not get a pass for having an innocent argv[0].
    """
    parts = shlex.split(command) if isinstance(command, str) else list(command)
    if not parts:
        return None

    # Each argument is re-split on whitespace before matching. `sh -c "nmap acme.io"`
    # arrives as one argv entry containing the whole scan, and checking only the
    # entry's basename let it through -- which is the single most obvious way to
    # smuggle a scanner past a denylist.
    for part in parts:
        for token in part.split():
            name = token.rsplit("/", 1)[-1].lower()
            if name in FORBIDDEN_BINARIES:
                return f"{name} is an active-scanning tool; this system is passive-only"

    lowered = " ".join(parts).lower()
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern in lowered:
            return f"{pattern.strip()!r} is an active probe against a prospect's service"
    return None


def assert_command_allowed(command: str | list[str]) -> None:
    """Raise :class:`PassiveOnlyViolation` if this command must never run.

    Call before any `subprocess` invocation. Nothing in the pipeline shells out today;
    this is here so that the first thing that does cannot quietly be a scanner.
    """
    reason = is_forbidden_command(command)
    if reason is not None:
        raise PassiveOnlyViolation(reason)
