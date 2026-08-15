# CindraLeads

Self-hosted, passive-only lead intelligence for [Cindrasec](https://cindrasec.com).

It answers one question every day: *which specific organizations have a dated and
evidenced reason, right now, to buy an attack-surface Snapshot, a Watch subscription, or
an AI/LLM security assessment — and who exactly do I talk to?*

It runs unattended on a single Raspberry Pi 5 (16 GB), does the bulk of its inference on
a local model, and delivers structured lead cards to Discord. A human decides who gets
contacted; the pipeline never sends outbound email.

## Two rules that shape everything

**Passive-only.** The system never touches a prospect's infrastructure beyond a
public-record lookup — DNS, Certificate Transparency, RDAP, licensed APIs, and
robots-respecting GETs of self-published pages. No port scans, no vulnerability
scanners, no brute-forcing, no authenticated requests, no SMTP probing. This is enforced
at the tool boundary and tested, because Cindrasec's entire pitch is *"no scan ever
starts without a signed Rules of Engagement."*

**No evidence, no lead.** Every trigger carries at least one reachable evidence URL with
a content hash and a timestamp. Unsourced fields are set to `None`, never guessed. A
fluent-sounding hallucinated lead is worse than no lead.

## Status

| Phase | State |
| --- | --- |
| 0 · Foundation | **Complete** — gate passed on real hardware: 100 jobs, `kill -9`, exactly once |
| 1 · Pi baseline | **Complete** — gate passed: 24 real pages, 100% schema-valid, 0 timeouts |
| 2 · Harvest | In progress — source registry done; egress, cache, budget, breakers next |
| 3–8 | Planned — see [`PLAN.md`](PLAN.md) |

Measured on the Pi 5 ([`docs/BENCHMARKS.md`](docs/BENCHMARKS.md), generated not hand-written):

| | Schema valid | p50 | p95 | Decode |
| --- | ---: | ---: | ---: | ---: |
| `qwen3:4b-instruct` | 100% | 64.6 s | 76.7 s | 3.6 tok/s |
| `llama3.2:3b` | 100% | 39.8 s | 60.0 s | 4.6 tok/s |

Decode costs ~11× a prefill token, which is why extraction output is bounded by the JSON
Schema itself rather than by asking the model nicely. Schema validity cannot separate
these two models — that is Phase 3's accuracy gate, not this one.

## Quickstart

```bash
make install
cp .env.example .env        # chmod 0600; fill in as later phases need keys
.venv/bin/cindra db migrate
make check                  # lint + mypy strict + 62 tests
make gate                   # the Phase 0 acceptance drill
```

## The Phase 0 gate

The pipeline's state and its job queue are one SQLite file in WAL mode. There is no
Redis and no Celery, because power loss on a Pi is a *when*, not an *if*, and a broker
is one more thing to babysit through it.

Durability comes from a single rule: **a stage's side effect and its queue completion
commit in the same transaction.**

```python
with store.tx() as conn:
    conn.execute("INSERT INTO results ...")   # the work
    queue.complete(job.job_id, conn=conn)     # the bookkeeping
```

Killed before the COMMIT, both roll back and the job's lease expires so another worker
picks it up. Killed after, both are durable and the job is never handed out again. There
is no window in between.

`make gate` proves it rather than asserting it: enqueue 100 jobs, spawn a real worker,
`SIGKILL` it mid-transaction three times, restart, and require exactly 100 side effects
across 100 distinct jobs with an empty dead-letter queue. The side-effect table's
PRIMARY KEY is the job id, so a duplicate would raise rather than pass quietly.

## Layout

```
PLAN.md                architecture review + phased build plan
CINDRALEADS_MASTER_PROMPT.md   the original spec, cited by section throughout
db/migrations/         schema source of truth (db/schema.sql is generated)
src/cindraleads/       models, store, queue, config, logging, cli
tests/unit/            fast tests
tests/integration/     the durability drill - real processes, real signals
```

## License

Proprietary. © Md. Azmol Haque Rony / Cindrasec.
