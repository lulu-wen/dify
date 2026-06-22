# Translation Benchmark

Quality + latency profiling for the Gateway / vLLM stack across `low` / `mid` / `high` tiers.

Drives an OpenAI-compatible chat-completions endpoint, records translations and timing, then scores each output against a curated reference set with COMET and chrF. Outputs a CSV + per-tier summary.

## Files

| File | Purpose |
|---|---|
| `translation_benchmark.py` | Main script — runs the fleet, scores, reports |
| `translation_testset.json` | 25-entry curated test set (general / base_station / legal × multi-lang) |
| `mock_ems_server.py` | Fake vLLM endpoint for plumbing tests without burning GPU |
| `test_translation_benchmark.py` | Unit tests (pytest) — pure functions + httpx.MockTransport |
| `README_BENCHMARK.md` | This file |

## Quick start

### 1. Plumbing test against mock EMS

Two terminals:

```bash
# Terminal 1 — start the fake EMS
python scripts/mock_ems_server.py --port 9090

# Terminal 2 — drive the benchmark against it
python scripts/translation_benchmark.py \
    --endpoint http://localhost:9090 \
    --api-key fake-key \
    --tier mock-low mock-mid mock-high \
    --output mock_results.csv \
    --skip-comet
```

The mock produces deliberately differentiated outputs per tier (truncated for low, slightly mangled for mid, perfect for high) so the CSV + summary should show the expected quality gradient even without a real model.

### 2. Real benchmark (when EMS is up)

```bash
python scripts/translation_benchmark.py \
    --endpoint http://localhost:8080 \
    --api-key bsa_your_real_key \
    --tier low mid high \
    --output results/run-$(date +%Y-%m-%d).csv
```

First run downloads the COMET model (~2 GB) — subsequent runs use cache.

### 3. Override tier configs at runtime

```jsonc
// tiers_custom.json
[
  {"name": "exp-13b", "model": "qwen-2.5-13b-int8", "max_tokens": 768},
  {"name": "exp-32b", "model": "qwen-2.5-32b-fp16", "max_tokens": 1024}
]
```

```bash
python scripts/translation_benchmark.py \
    --tier-json tiers_custom.json \
    --tier exp-13b exp-32b \
    ...
```

## Test set expansion

`translation_testset.json` ships with **25 entries across 3 categories × 6 language pairs**. To extend:

1. Open the JSON.
2. Append new objects to the `cases` array. Each needs:
   - `id` (unique)
   - `src_lang`, `src_text`
   - `ref_lang`, `ref_text` (the **reference** translation — quality scoring's ground truth)
   - `category` (existing or new)
3. The benchmark groups by category automatically — no script changes needed.

**Categories ship with**:

- `general` (7 entries) — Everyday conversational content. Baseline.
- `base_station` (8 entries) — 5G / RAN terminology: PRACH, OFDM, gNodeB, handover, beamforming, BLER, F1AP, S-NSSAI. Stress-tests technical vocab.
- `legal` (10 entries) — Contractual / regulatory: indemnification, force majeure, jurisdiction, arbitration, GDPR. Famously hard because legal concepts don't map 1:1 across common-law / civil-law systems.

Reference translations should be **high quality**. Either:
- Write them yourself (most accurate, slowest)
- Use a top-tier model (GPT-4 / Claude 3.5 Sonnet) to draft, then human-review

## Output

### CSV columns

```
tier_name, case_id, category, src_lang, ref_lang,
ttft_ms, total_ms, completion_tokens,
comet, chrf, error,
src_text, ref_text, output
```

One row per (tier, case). Filter / pivot in pandas, Excel, or [datasette](https://datasette.io) to slice by category, language pair, etc.

### Console summary

```
TIER: mid
----------------------------------------------------------------------
  cases:           25 (0 failed)
  COMET (avg):     0.847
  chrF  (avg):     61.32
  TTFT  ms p50/p99: 180 / 420
  total ms p50/p99: 890 / 2100
  by category:
    general        n=7   COMET=0.910  avg_total=720ms
    base_station   n=8   COMET=0.780  avg_total=1100ms   ← 技術術語明顯差
    legal          n=10  COMET=0.820  avg_total=950ms
```

The **per-category breakdown** is the most actionable signal — a tier that's "fine" on general but tanks on base_station / legal needs domain prompt engineering or a different model.

## Metrics — what each tells you

| Metric | Range | Interpretation |
|---|---|---|
| **COMET** | 0.0–1.0 | LLM-based, closest to human judgment. **Use this as the headline.** |
| **chrF** | 0–100 | Character n-gram F-score. Robust for low-resource langs. Faster than COMET. |
| **TTFT** | ms | Time to first token. Dominant UX metric for voice translation. |
| **Total latency** | ms | End-to-end. Set tier latency budgets against this. |
| **completion_tokens** | int | Reported by upstream. Sanity check that `max_tokens` isn't truncating. |

Skip COMET (`--skip-comet`) for fast plumbing iteration; skip chrF (`--skip-chrf`) similarly. Both can be re-run later from the CSV (translations are persisted).

## Testing the benchmark itself

```bash
uv run pytest scripts/test_translation_benchmark.py -v
```

Tests cover:
- Prompt building (language-name mapping, output format)
- Test-set loading (schema + uniqueness)
- Tier resolution (built-in + custom JSON override)
- CSV writer (all columns present)
- Percentile math
- `translate_one()` against `httpx.MockTransport` (happy path, non-2xx, transport error, non-numeric usage value)
- chrF computation (perfect vs garbage translation)
- Summary printer (empty results don't crash, tier grouping works)

The integration test relies on `sacrebleu` for chrF; COMET tests aren't in the suite (model is 2 GB).

## Dependencies

Hard requirements (in main script):
- `httpx` — already in the gateway venv
- `fastapi` + `uvicorn` — for the mock server, already in the venv

Soft requirements (auto-skip if missing):
- `sacrebleu` for chrF: `pip install sacrebleu`
- `unbabel-comet` for COMET: `pip install unbabel-comet` (heavy — ~2 GB download on first use)

## Next steps after a benchmark run

1. **Confirm tier hypothesis** — does Low/Mid/High actually separate in the Pareto plot?
2. **Expose the winning configs** through the gateway's `quality_tier` request param (or use latency budget to auto-select).
3. **Document** the chosen configs in the Notion design doc.
4. **Track over time** — re-run after every vLLM upgrade / model swap and diff the CSVs.

## Related

- Design rationale: [Notion · Doc: vLLM Operator Params + 翻譯品質 Tier 設計](https://app.notion.com/p/387ecb37c4928129be18c91d60f835c0)
- Gateway PR #13 thin-proxy mode: `feat/ai-sdk-gateway-pr13-thin-proxy` branch
