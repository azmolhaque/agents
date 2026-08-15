# BENCHMARKS

> **Status: NOT YET MEASURED.**
>
> This file is a placeholder. It will be **overwritten** by
> `scripts/benchmark_models.py` (`make bench`) the first time it runs on the Pi.
> Nothing below is a measurement — there are deliberately no invented numbers here,
> because the entire reason this file exists is that the estimates in the master prompt
> were not to be trusted.

## Why it is empty

The benchmark has two hard requirements that the build container cannot satisfy:

1. **It must run on the actual Pi 5.** Tokens/sec, p95 latency and peak SoC temperature
   are properties of BCM2712 under a passive-or-active cooler, not of an x86 CI runner.
   A number measured anywhere else is worse than no number, because it looks credible.
2. **It needs the fixture corpus.** `tests/fixtures/html/` is empty in this checkout.
   The container's network policy answers 403 to `CONNECT` for general web hosts, so
   the pages could not be gathered here. See `tests/fixtures/README.md`.

## How to produce it

On the Pi:

```bash
# 1. Ollama and the two resident models (PLAN.md 2.2: no third router model)
curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp deploy/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama

ollama pull qwen3:4b-instruct
ollama pull bge-m3

# 2. The fixture corpus (robots-respecting, 3s apart, real UA)
python scripts/fetch_fixtures.py

# 3. Measure
make bench
```

Then commit the regenerated file.

## What the run will record

| Column | Meaning |
| --- | --- |
| Schema valid | Fraction parsing into `Candidate` under Ollama `format`. **The Phase 1 gate is >= 95%.** |
| p50 / p95 latency | Wall clock per page, end to end, including prompt eval |
| Median tok/s | Decode throughput — the number that decides whether 90 s/lead is reachable |
| Peak temp | Highest SoC temperature seen during the run |
| Throttled | Whether `vcgencmd get_throttled` ever reported an active bit |

Models that are not installed are listed explicitly rather than skipped. If
`qwen3:4b-instruct` does not resolve as a tag, the table will say so and
`llama3.2:3b` (already present on the Pi) is the measured fallback — the plan does not
get to quietly assume a model exists.

## What it deliberately does not measure

Field-level **accuracy**. A model can be 100% schema-valid and wrong about every field;
grammar-constrained decoding guarantees shape, not truth. Accuracy needs the
hand-labelled golden set and is the **Phase 3** gate, split three ways per PLAN.md 2.11:

| Metric | Target |
| --- | --- |
| Schema validity | >= 98% |
| Critical fields (`canonical_domain`, `display_name`, trigger present, evidence URL present) | >= 90% |
| Soft fields (`employee_band`, `industry`, funding amount) | >= 70%, unsourced => `null` |

## Pre-flight checks worth recording alongside the table

```bash
vcgencmd measure_temp          # needs the service user in the `video` group
vcgencmd get_throttled         # 0x0 before starting, or the baseline is already bad
free -m                        # >= 4 GB headroom with both models resident
lsblk -o NAME,ROTA,TRAN        # confirm the DB is on NVMe, not microSD
ollama ps                      # confirm 2 models loaded, not 3 thrashing
```
