"""The ComplianceGate: section 12 as executable rules with veto power.

This is the module that protects the business, so it is written to be *readable by a
non-programmer under scrutiny*. Each rule is a small named function returning a bool,
they are listed in one table, and the verdict records every check by name — passed or
failed. If someone ever asks "how do you know you did not contact a government agency",
the answer is a row in `ComplianceVerdict.checks`, not a claim.

Two deliberate properties:

* **Every rule is checked, every time.** No short-circuit on the first veto. A lead
  that fails three rules records three, which is what makes the veto reasons useful for
  tuning the ICP rather than just for blocking.
* **A veto is loud.** Vetoed leads go to `quarantine` with the reason. The master
  prompt is explicit that they are never silently dropped, and silence here would mean
  never learning that a whole class of prospect is being rejected by mistake.

The gate cannot approve outbound contact, because nothing in this system sends any. The
Dispatcher writes a card to Discord and a human decides. That human-in-the-loop is the
control, and no amount of scoring replaces it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.logging import get_logger
from cindraleads.models import ComplianceVerdict, to_iso, utcnow

__all__ = ["RULES", "ComplianceGate", "LeadFacts"]

log = get_logger("cindraleads.compliance")


@dataclass(frozen=True)
class LeadFacts:
    """What the gate is allowed to look at.

    A narrow, explicit input rather than the whole `Lead`: a rule that can reach
    arbitrary state is a rule nobody can reason about, and this is the one module where
    "what exactly does this check" has to have a short answer.
    """

    canonical_domain: str
    display_name: str
    employee_band: str | None = None
    industry: str | None = None
    country: str | None = None
    trigger_codes: tuple[str, ...] = ()
    evidence_urls: tuple[str, ...] = ()
    contact_emails: tuple[str, ...] = ()
    has_business_affiliation: bool = True
    suppressed: bool = False
    sectors_excluded: tuple[str, ...] = ()
    max_employees: int = 1000


# Free-mail providers. A prospect reachable only at one of these is an individual, and
# section 12 is explicit that this is B2B only.
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "fastmail.com",
        "tutanota.com",
        "hey.com",
    }
)

_BAND_TO_MAX: dict[str, int] = {
    "1-10": 10,
    "11-50": 50,
    "51-200": 200,
    "201-1000": 1000,
    "1000+": 100_000,
}

# Sector words that mean "never contact", matched against industry text.
_GOV_WORDS = ("government", "ministry", "municipal", "federal", "defence", "defense", "military")
_CNI_WORDS = ("critical infrastructure", "power grid", "nuclear", "water utility", "air traffic")

# Suffixes reserved for government, by registry policy rather than by convention. A
# domain under one of these is a public body as a matter of fact, which is stronger
# evidence than any word a model might put in an `industry` field.
_GOV_SUFFIXES = (
    "gov",
    "mil",
    "gov.uk",
    "gov.au",
    "gov.in",
    "gov.bd",
    "gov.pk",
    "gov.lk",
    "gov.np",
    "gov.sg",
    "gov.za",
    "govt.nz",
    "go.jp",
    "go.kr",
    "go.id",
    "gc.ca",
    "gouv.fr",
    "europa.eu",
)
_COMPETITOR_WORDS = (
    "penetration testing",
    "pentest",
    "security consultancy",
    "security vendor",
    "offensive security",
    "red team",
    "redteam",
    "redteaming",
    "bug bounty",
    "vapt",
    "managed security",
    "soc provider",
)


# --------------------------------------------------------------------- the rules
#
# Each returns True when the lead is ACCEPTABLE. Named for the property they assert,
# so a failing check reads as the thing that is not true.


def has_evidence(facts: LeadFacts) -> bool:
    """No evidence, no lead. The rule the whole project rests on."""
    return bool(facts.evidence_urls)


def has_trigger(facts: LeadFacts) -> bool:
    """Fit alone is noise. A dated reason to call is the product."""
    return bool(facts.trigger_codes)


def not_suppressed(facts: LeadFacts) -> bool:
    return not facts.suppressed


def is_business_not_individual(facts: LeadFacts) -> bool:
    return facts.has_business_affiliation


def no_personal_email(facts: LeadFacts) -> bool:
    """Business contacts only, never personal addresses."""
    return not any(
        email.rsplit("@", 1)[-1].lower() in PERSONAL_EMAIL_DOMAINS
        for email in facts.contact_emails
        if "@" in email
    )


def under_employee_ceiling(facts: LeadFacts) -> bool:
    """An enterprise with a named CISO and an in-house red team is not a prospect."""
    if facts.employee_band is None:
        return True  # silence is not evidence of size
    return _BAND_TO_MAX.get(facts.employee_band, 0) <= facts.max_employees


def not_government_or_cni(facts: LeadFacts) -> bool:
    """Government and critical infrastructure, by domain first and prose second.

    The word match alone rested on `industry` and `display_name` -- text a 4B model
    wrote from a web page. For a hard exclude that is the wrong evidence: whether we
    may contact a government agency should not depend on the extractor choosing to type
    "government" into a field it often leaves empty. `nyc.gov` arrived from the hiring
    thread with the display name "New York City Public Interest Tech", which contains
    none of the words below.

    A government TLD is definitional rather than inferred, so it is checked first.
    """
    if _has_government_tld(facts.canonical_domain or ""):
        return False
    text = f"{facts.industry or ''} {facts.display_name}".lower()
    return not any(word in text for word in (*_GOV_WORDS, *_CNI_WORDS))


def _has_government_tld(domain: str) -> bool:
    return _has_suffix(domain, _GOV_SUFFIXES)


def _has_suffix(domain: str, suffixes: tuple[str, ...]) -> bool:
    """Suffix match, never substring. `govinda.io` and `datagov.com` are ordinary
    companies, and `eduflow.com` is a real prospect -- a rule that vetoed them would
    shrink the corpus silently, which is the failure mode a hard exclude can least
    afford."""
    host = domain.strip().lower().strip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


_ACADEMIC_WORDS = ("university", "college", "polytechnic", "faculty of")

# Suffixes reserved for accredited education by registry policy, exactly like
# `_GOV_SUFFIXES`. A domain under one of these is a school as a matter of fact, which
# is the only evidence strong enough here: `iutoic-dhaka.edu` arrived as "Department of
# Computer Science and Engineering", which contains none of the words above.
_ACADEMIC_SUFFIXES = (
    "edu",
    "ac.uk",
    "ac.bd",
    "ac.in",
    "ac.jp",
    "ac.kr",
    "ac.nz",
    "ac.za",
    "ac.lk",
    "edu.au",
    "edu.bd",
    "edu.in",
    "edu.pk",
    "edu.np",
    "edu.sg",
    "edu.my",
)


def not_academic(facts: LeadFacts) -> bool:
    """A university is not a B2B prospect, and `github_orgs` finds them by design.

    The location search asks who is *there*, and what is there includes the computer
    science department. Predicted when the engine shipped -- "an org account with a
    site is a team, not a payroll; an OSS collective and a university lab both qualify"
    -- and confirmed on the first 29 companies: `mbstu.ac.bd` and `iutoic-dhaka.edu`.

    Suffix first and words second, the same shape and the same reasoning as
    `not_government_or_cni`: the TLD is a fact about the organisation, the `industry`
    field is a 4B model's guess about it.

    **Deliberately does not cover the nonprofits in that same batch.** `arced.foundation`
    and a student rover team at `bracu-mongoltori.com` are also out of ICP, and no safe
    general rule reaches them -- bare "foundation" would veto a "Foundation Health",
    which is primary ICP, the same trap as bare "media" in the publication list and the
    name-does-not-match-domain rule that `Rover · rtrvr.ai` killed. A rule that cannot
    be made safe is left unwritten rather than shipped narrow and trusted wide.
    """
    if _has_suffix(facts.canonical_domain or "", _ACADEMIC_SUFFIXES):
        return False
    text = f"{facts.industry or ''} {facts.display_name}".lower()
    return not any(word in text for word in _ACADEMIC_WORDS)


def not_a_competitor(facts: LeadFacts) -> bool:
    """Security vendors are competitors' customers, not ours."""
    text = f"{facts.industry or ''} {facts.display_name}".lower()
    return not any(word in text for word in _COMPETITOR_WORDS)


def not_an_excluded_sector(facts: LeadFacts) -> bool:
    text = f"{facts.industry or ''} {facts.display_name}".lower()
    return not any(sector.lower() in text for sector in facts.sectors_excluded)


def has_canonical_domain(facts: LeadFacts) -> bool:
    return bool(facts.canonical_domain)


RULES: dict[str, Callable[[LeadFacts], bool]] = {
    "has_evidence": has_evidence,
    "has_trigger": has_trigger,
    "not_suppressed": not_suppressed,
    "is_business_not_individual": is_business_not_individual,
    "no_personal_email": no_personal_email,
    "under_employee_ceiling": under_employee_ceiling,
    "not_government_or_cni": not_government_or_cni,
    "not_a_competitor": not_a_competitor,
    "not_academic": not_academic,
    "not_an_excluded_sector": not_an_excluded_sector,
    "has_canonical_domain": has_canonical_domain,
}


@dataclass
class ComplianceGate:
    excluded_sectors: tuple[str, ...] = ()
    max_employees: int = 1000
    suppressed_domains: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, config: Settings | None = None) -> ComplianceGate:
        cfg = config or settings()
        data = load_yaml("icp", base=cfg.resolve(cfg.config_dir))
        anti = (data.get("profile") or {}).get("anti_icp") or {}
        return cls(
            excluded_sectors=tuple(str(s) for s in anti.get("exclude_sectors") or ()),
            max_employees=int(anti.get("max_employees", 1000)),
        )

    def fingerprint(self) -> str:
        """Identity of the veto *rules*, for the scoring stamp.

        A veto is arithmetic: `anti_icp` is -100, so changing `exclude_sectors` changes
        what score a lead gets, from 66 to 0. But the list lives in `icp.yaml` and
        `ScoringConfig.fingerprint()` hashes `scoring.yaml`, so **editing the veto list
        has never invalidated a single stored lead.** `schneier.com` sat at Tier B 66
        with `industry = "security consultancy"` -- a sector already on the list --
        because the lead was scored before `industry` was populated, and nothing could
        find it afterwards: the calibration matched, no trigger moved, the angle was
        present. Same shape as `RETIREMENT_RULES` and `prose_version`.

        `suppressed_domains` is deliberately *not* in here. It changes every time a
        human types `cindra suppress`, and rescoring 780 leads to reject one domain is
        the wrong trade -- suppression is already applied live by `worklist` and at plan
        time by the Scout. What belongs here is the part that is a *rule* rather than a
        row: which sectors we refuse, and how large is too large.

        **And for one build it hashed the veto config while claiming to hash the veto
        rules.** Adding `not_academic` to `RULES` changes what a lead scores by 100
        points and moved neither `excluded_sectors` nor `max_employees`, so the stamp
        matched, `enqueue_stale_scores` found nothing to do, and `mbstu.ac.bd` would
        have kept its score forever -- the *identical* defect this function was written
        to fix, one level up, found by reading the docstring against its own last line.

        So the rule names are in it, and so are the literals the rules match on: adding
        `ac.pk` to `_ACADEMIC_SUFFIXES` is as much a veto change as adding a sector.
        What still escapes is a change to a rule's *body* that touches no constant --
        said out loud rather than papered over, because `inspect.getsource` would
        invalidate the whole corpus on a reworded comment, which is the trade that makes
        a staleness signal get ignored.
        """
        shape = "|".join(
            (
                str(sorted(s.lower() for s in self.excluded_sectors)),
                str(self.max_employees),
                str(sorted(RULES)),
                str(
                    (
                        *_GOV_SUFFIXES,
                        *_GOV_WORDS,
                        *_CNI_WORDS,
                        *_ACADEMIC_SUFFIXES,
                        *_ACADEMIC_WORDS,
                        *_COMPETITOR_WORDS,
                    )
                ),
            )
        )
        return hashlib.sha256(shape.encode()).hexdigest()[:16]

    def load_suppression(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT value FROM suppression_list WHERE kind = 'domain'").fetchall()
        self.suppressed_domains = frozenset(str(r["value"]).strip().lower() for r in rows)

    def review(self, facts: LeadFacts) -> ComplianceVerdict:
        """Run every rule. No short-circuit — a lead failing three records three."""
        enriched = LeadFacts(
            **{
                **facts.__dict__,
                "suppressed": facts.suppressed
                or facts.canonical_domain.lower() in self.suppressed_domains,
                "sectors_excluded": facts.sectors_excluded or self.excluded_sectors,
                "max_employees": self.max_employees,
            }
        )
        checks = {name: bool(rule(enriched)) for name, rule in RULES.items()}
        vetoes = [name for name, ok in checks.items() if not ok]
        return ComplianceVerdict(
            passed=not vetoes,
            checks=checks,
            basis="legitimate_interest_b2b",
            vetoes=vetoes,
            reviewed_at=utcnow(),
        )

    @staticmethod
    def quarantine(conn: sqlite3.Connection, *, subject_id: str, verdict: ComplianceVerdict) -> str:
        """A vetoed lead is recorded, never silently dropped.

        Silence would mean never discovering that a whole class of prospect is being
        rejected by a rule that is subtly wrong.
        """
        quarantine_id = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO quarantine (quarantine_id, subject_kind, subject_id, reason_code, "
            "detail, created_at) VALUES (?,?,?,?,?,?)",
            (
                quarantine_id,
                "lead",
                subject_id,
                ",".join(verdict.vetoes)[:200],
                f"basis={verdict.basis}",
                to_iso(utcnow()),
            ),
        )
        log.info("compliance_veto", subject_id=subject_id, vetoes=verdict.vetoes)
        return quarantine_id
