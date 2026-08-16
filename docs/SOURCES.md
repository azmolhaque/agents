# Sources, and why each one is allowed

Every source the pipeline touches, with its legality class and the reason it is
permitted under the passive-only rule. This file exists so the question "how do you
know you never scanned anyone?" has a document behind it rather than an assurance.

The enforcement is in code, not here: `config/sources.yaml` refuses to load a source
with no declared legality class, and `src/cindraleads/sources/http.py` is the only
function in the system that makes an outbound request.

## Legality classes

| Class | Meaning | Examples |
| --- | --- | --- |
| `public_record` | A public register or log, queried as a register | crt.sh, RDAP, public DNS |
| `public_web` | A page the company published for anyone to read | their homepage, `/about`, `/.well-known/security.txt` |
| `licensed_api` | An API we are entitled to call under its own terms | SerpAPI, GitHub, HN Algolia |
| `first_party` | Our own data | inbound mail to contact@cindrasec.com |

## Discovery

| Source | Class | What it answers | Cost |
| --- | --- | --- | --- |
| `hn_algolia` | licensed_api | Who announced something recently? | free |
| `github_api` | licensed_api | Whose public code imports an agent framework? | free |
| `serpapi_*` | licensed_api | Who is posting a pentest brief? | 1 credit, capped at 8/day |

## Enrichment

| Source | Class | What it answers | Trigger |
| --- | --- | --- | --- |
| `crtsh` | public_record | How fast is their certificate estate growing? | T7_SURFACE_SPRAWL |
| public DNS | public_record | What mail policy do they publish? | T8_HYGIENE_GAP |
| `rdap` | public_record | How old is the domain? | context only |
| `company_site` | public_web | What do they say they do, and who answers mail? | contacts |
| `greenhouse_boards`, `lever_postings`, `ashby_postings` | public_web | Who are they hiring? | T3, T4, T5, T11 |

The ATS boards are why the free tier works at all. The master prompt routed T3/T4/T5/T11
through paid search; every one of these vendors publishes the same data as public JSON
once the company is known, so discovery is the only thing that ever needs to cost money.

## What is never done

Not a matter of configuration — these have no code path:

- No port or service scanning of any kind.
- No vulnerability scanning, fuzzing, or content discovery.
- No authenticated request, credential test, or session against a prospect.
- No SMTP `VRFY` or `RCPT`. Email verification stops at an MX lookup; `contacts.py`
  imports no socket library, and a test asserts that.
- Nothing behind a login, a paywall, or an explicit ToS prohibition.
- No request rate that could degrade a prospect's service: 6 fetches per domain per
  rolling 24 h, at least 3 s apart, robots-respecting.

`src/cindraleads/passive.py` carries a denylist of scanner binaries the runtime refuses
to shell out to. Nothing shells out today; the guard exists so the first thing that does
cannot quietly be a scanner.

## What a DNS finding is, and is not

`T8_HYGIENE_GAP` reads published records: SPF, DMARC, DNSSEC, MX. A `DMARC p=none`
result means *the policy they published is none*. It is an internal prioritisation hint.
It never appears in outreach as something we discovered about their security, and the
embed builder is tested against the wording.

A field we could not read contributes nothing. "Unknown" and "absent" are different
values throughout, because reporting "no SPF record" after a resolver timeout would put
a false claim on a card a human may act on.
