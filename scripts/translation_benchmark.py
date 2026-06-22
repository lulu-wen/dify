"""Translation quality + latency benchmark for the AI SDK Gateway / vLLM.

Drives a fleet of tier configurations against an OpenAI-compatible chat-
completions endpoint, records translations + latency, computes COMET / chrF
scores against the reference translations, and emits a CSV report plus
per-tier summary stats.

Usage
-----

Against a real gateway (with the EMS upstream live)::

    uv run python scripts/translation_benchmark.py \\
        --endpoint http://localhost:8080 \\
        --api-key bsa_test_local_abc... \\
        --tier low mid high \\
        --output results/run-2026-06-22.csv

Against the mock EMS for dev / unit-test wiring::

    # Terminal 1
    uv run python scripts/mock_ems_server.py --port 9090

    # Terminal 2
    uv run python scripts/translation_benchmark.py \\
        --endpoint http://localhost:9090 \\
        --api-key fake-key \\
        --tier mock-low mock-mid mock-high \\
        --skip-comet  # mock outputs aren't worth scoring; just verify wiring

Add ``--skip-comet`` to bypass the (heavy) COMET model download when
iterating on plumbing. ``--limit N`` runs only the first N test cases.

Output
------

* ``<output>.csv`` — one row per (tier, test_case): src, ref, output,
  TTFT, total latency, COMET, chrF, error.
* Console summary: per-tier averages with p50/p99 latency, plus a
  per-category breakdown (general / base_station / legal).

Design notes
------------

* Streaming is forced ON so we can measure TTFT separately from total
  latency. TTFT is the dominant UX metric for voice translation.
* COMET takes ~1-2s per case on CPU. For a 25-case × 3-tier run that's
  ~75s of scoring time, dwarfed by inference. On GPU it's <100ms each.
* Errors don't abort the run — failed cases get error text in the CSV
  and are excluded from average COMET (but counted as failures in the
  summary).
* The "tier" abstraction is just (model + sampling params). When the
  EMS catalog stabilizes, replace the ``BUILTIN_TIERS`` table with the
  real model IDs (e.g. "qwen-2.5-7b-int8") and ship.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


# --------------------------------------------------------------------------- #
# Tier configurations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TierConfig:
    """One row in the quality-vs-latency grid.

    ``name`` is the user-facing tier label (low / mid / high). ``model``
    is the model id the upstream knows. The sampling params are passed
    verbatim into the OpenAI chat-completions body.
    """

    name: str
    model: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 512
    description: str = ""


# Initial-guess catalog. Override via --tier-json for custom runs.
# Mock tiers point at scripts/mock_ems_server.py; real tiers must match the
# model ids your vLLM is serving (check /v1/models).
BUILTIN_TIERS: dict[str, TierConfig] = {
    "mock-low": TierConfig(
        name="mock-low",
        model="mock-low",
        max_tokens=128,
        description="Mock EMS — simulated low-quality translation",
    ),
    "mock-mid": TierConfig(
        name="mock-mid",
        model="mock-mid",
        max_tokens=512,
        description="Mock EMS — simulated mid-quality translation",
    ),
    "mock-high": TierConfig(
        name="mock-high",
        model="mock-high",
        max_tokens=1024,
        description="Mock EMS — simulated high-quality (reference) translation",
    ),
    "low": TierConfig(
        name="low",
        model="gemma-3n-e4b",  # adjust to whatever your small/quantized model is
        max_tokens=128,
        description="Small model, int4 quant — fast real-time captioning",
    ),
    "mid": TierConfig(
        name="mid",
        model="gemma-3-9b",  # adjust
        max_tokens=512,
        description="Mid model, int8 — default translation tier",
    ),
    "high": TierConfig(
        name="high",
        model="qwen-2.5-32b",  # adjust
        max_tokens=1024,
        description="Large model, fp16 — accurate document translation",
    ),
}


# --------------------------------------------------------------------------- #
# Test set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TestCase:
    """One source/reference pair to translate."""

    id: str
    src_lang: str
    src_text: str
    ref_lang: str
    ref_text: str
    category: str


def load_testset(path: Path) -> list[TestCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [TestCase(**c) for c in raw["cases"]]


# Friendly target-language names for prompt construction.
LANG_NAMES: dict[str, str] = {
    "zh-TW": "Traditional Chinese",
    "zh": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "pt-BR": "Brazilian Portuguese",
    "hi": "Hindi",
    "ko": "Korean",
    "de": "German",
}


def build_translation_prompt(case: TestCase) -> str:
    target = LANG_NAMES.get(case.ref_lang, case.ref_lang)
    return (
        f"Translate the following text into {target}. "
        f"Output ONLY the translation, no explanations, no quotes.\n\n"
        f"Text: {case.src_text}"
    )


# --------------------------------------------------------------------------- #
# Per-request execution
# --------------------------------------------------------------------------- #


@dataclass
class TranslationResult:
    tier_name: str
    case_id: str
    src_lang: str
    ref_lang: str
    category: str
    src_text: str
    ref_text: str
    output: str
    ttft_ms: float
    total_ms: float
    completion_tokens: int = 0
    error: str = ""
    comet: float | None = None
    chrf: float | None = None


async def translate_one(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    tier: TierConfig,
    case: TestCase,
    timeout_s: float,
) -> TranslationResult:
    """Stream a translation request and capture TTFT + total + output."""
    body: dict[str, Any] = {
        "model": tier.model,
        "messages": [{"role": "user", "content": build_translation_prompt(case)}],
        "temperature": tier.temperature,
        "top_p": tier.top_p,
        "max_tokens": tier.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # Base64-encode the reference so httpx (ASCII-only headers by default)
    # accepts CJK / emoji / other non-Latin1 reference texts. The mock EMS
    # decodes this header to fake quality-differentiated outputs; real
    # upstreams ignore the unknown header.
    ref_b64 = base64.b64encode(case.ref_text.encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Benchmark-Ref-Text-B64": ref_b64,
    }

    t0 = time.perf_counter()
    ttft_ms: float | None = None
    output_parts: list[str] = []
    completion_tokens = 0

    try:
        async with client.stream(
            "POST",
            f"{endpoint.rstrip('/')}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=timeout_s,
        ) as resp:
            if not resp.is_success:
                err_body = await resp.aread()
                preview = err_body.decode(errors="replace")[:300]
                return _failed(tier, case, t0, f"HTTP {resp.status_code}: {preview}")

            async for raw in resp.aiter_lines():
                if not raw or not raw.startswith("data: "):
                    continue
                tail = raw[len("data: "):].strip()
                if tail == "[DONE]":
                    break
                try:
                    chunk = json.loads(tail)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    # Terminal usage chunk per OpenAI include_usage spec.
                    usage = chunk.get("usage") or {}
                    try:
                        completion_tokens = int(usage.get("completion_tokens") or 0)
                    except (TypeError, ValueError):
                        completion_tokens = 0
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    output_parts.append(delta)
    except httpx.TimeoutException as e:
        return _failed(tier, case, t0, f"timeout: {e}")
    except httpx.RequestError as e:
        return _failed(tier, case, t0, f"transport: {e}")

    total_ms = (time.perf_counter() - t0) * 1000
    return TranslationResult(
        tier_name=tier.name,
        case_id=case.id,
        src_lang=case.src_lang,
        ref_lang=case.ref_lang,
        category=case.category,
        src_text=case.src_text,
        ref_text=case.ref_text,
        output="".join(output_parts).strip(),
        ttft_ms=ttft_ms if ttft_ms is not None else total_ms,
        total_ms=total_ms,
        completion_tokens=completion_tokens,
    )


def _failed(
    tier: TierConfig, case: TestCase, t0: float, error: str
) -> TranslationResult:
    return TranslationResult(
        tier_name=tier.name,
        case_id=case.id,
        src_lang=case.src_lang,
        ref_lang=case.ref_lang,
        category=case.category,
        src_text=case.src_text,
        ref_text=case.ref_text,
        output="",
        ttft_ms=0.0,
        total_ms=(time.perf_counter() - t0) * 1000,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Quality scoring
# --------------------------------------------------------------------------- #


def compute_chrf(results: list[TranslationResult]) -> None:
    """Annotate each result with chrF score in place. Uses sacrebleu."""
    try:
        from sacrebleu import CHRF
    except ImportError:
        print(
            "chrF skipped — install with `pip install sacrebleu` to enable.",
            file=sys.stderr,
        )
        return
    metric = CHRF()
    for r in results:
        if r.error or not r.output:
            continue
        try:
            r.chrf = metric.sentence_score(r.output, [r.ref_text]).score
        except Exception as e:
            print(f"chrF failed for {r.case_id}: {e}", file=sys.stderr)


def compute_comet(results: list[TranslationResult], *, batch_size: int = 8) -> None:
    """Annotate each result with COMET score in place. Heavy; needs unbabel-comet."""
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError:
        print(
            "COMET skipped — install with `pip install unbabel-comet` to enable.",
            file=sys.stderr,
        )
        return

    scorable = [r for r in results if not r.error and r.output]
    if not scorable:
        print("No scorable results for COMET.", file=sys.stderr)
        return

    print(
        f"Loading COMET model wmt22-comet-da (~2GB on first run)…",
        file=sys.stderr,
    )
    model_path = download_model("Unbabel/wmt22-comet-da")
    model = load_from_checkpoint(model_path)

    payload = [
        {"src": r.src_text, "mt": r.output, "ref": r.ref_text} for r in scorable
    ]
    print(f"Scoring {len(payload)} translations with COMET…", file=sys.stderr)
    output = model.predict(payload, batch_size=batch_size, progress_bar=False)
    scores = output.scores if hasattr(output, "scores") else output["scores"]
    for r, s in zip(scorable, scores, strict=True):
        r.comet = float(s)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def write_csv(results: list[TranslationResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "tier_name", "case_id", "category", "src_lang", "ref_lang",
        "ttft_ms", "total_ms", "completion_tokens",
        "comet", "chrf", "error",
        "src_text", "ref_text", "output",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))


def print_summary(results: list[TranslationResult]) -> None:
    by_tier: dict[str, list[TranslationResult]] = defaultdict(list)
    for r in results:
        by_tier[r.tier_name].append(r)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for tier_name in sorted(by_tier):
        rows = by_tier[tier_name]
        successes = [r for r in rows if not r.error]
        failures = [r for r in rows if r.error]
        comet_scores = [r.comet for r in successes if r.comet is not None]
        chrf_scores = [r.chrf for r in successes if r.chrf is not None]
        ttfts = [r.ttft_ms for r in successes]
        totals = [r.total_ms for r in successes]

        print(f"\nTIER: {tier_name}")
        print("-" * 70)
        print(f"  cases:           {len(rows)} ({len(failures)} failed)")
        if comet_scores:
            print(f"  COMET (avg):     {statistics.mean(comet_scores):.3f}")
        if chrf_scores:
            print(f"  chrF  (avg):     {statistics.mean(chrf_scores):.2f}")
        if ttfts:
            print(
                f"  TTFT  ms p50/p99: {_pct(ttfts, 50):.0f} / {_pct(ttfts, 99):.0f}"
            )
        if totals:
            print(
                f"  total ms p50/p99: {_pct(totals, 50):.0f} / {_pct(totals, 99):.0f}"
            )

        # Per-category breakdown
        by_cat: dict[str, list[TranslationResult]] = defaultdict(list)
        for r in successes:
            by_cat[r.category].append(r)
        if len(by_cat) > 1:
            print("  by category:")
            for cat in sorted(by_cat):
                rs = by_cat[cat]
                cs = [r.comet for r in rs if r.comet is not None]
                ts = [r.total_ms for r in rs]
                cstr = f"{statistics.mean(cs):.3f}" if cs else "—"
                tstr = f"{statistics.mean(ts):.0f}ms" if ts else "—"
                print(f"    {cat:<14} n={len(rs):<3} COMET={cstr}  avg_total={tstr}")

        if failures:
            print("  failures:")
            for r in failures[:3]:
                print(f"    {r.case_id}: {r.error[:80]}")
            if len(failures) > 3:
                print(f"    … and {len(failures) - 3} more")


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs_sorted = sorted(xs)
    k = (len(xs_sorted) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(xs_sorted) - 1)
    return xs_sorted[f] + (xs_sorted[c] - xs_sorted[f]) * (k - f)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def run_benchmark(
    *,
    endpoint: str,
    api_key: str,
    tiers: list[TierConfig],
    cases: list[TestCase],
    concurrency: int,
    timeout_s: float,
) -> list[TranslationResult]:
    results: list[TranslationResult] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def one(tier: TierConfig, case: TestCase) -> TranslationResult:
            async with sem:
                return await translate_one(
                    client=client,
                    endpoint=endpoint,
                    api_key=api_key,
                    tier=tier,
                    case=case,
                    timeout_s=timeout_s,
                )

        # Group by tier so progress bar makes sense.
        for tier in tiers:
            print(
                f"\n▶ Running tier '{tier.name}' ({tier.model}) — "
                f"{len(cases)} cases, concurrency={concurrency}",
                file=sys.stderr,
            )
            tasks = [one(tier, case) for case in cases]
            done = 0
            for fut in asyncio.as_completed(tasks):
                r = await fut
                results.append(r)
                done += 1
                if done % 5 == 0 or done == len(tasks):
                    print(f"  {done}/{len(tasks)}", file=sys.stderr)

    return results


def parse_tiers(spec: list[str], custom_path: Path | None) -> list[TierConfig]:
    catalog = dict(BUILTIN_TIERS)
    if custom_path:
        raw = json.loads(custom_path.read_text(encoding="utf-8"))
        for entry in raw:
            cfg = TierConfig(**entry)
            catalog[cfg.name] = cfg
    out: list[TierConfig] = []
    for name in spec:
        if name not in catalog:
            raise SystemExit(
                f"unknown tier '{name}'. Available: {sorted(catalog)}"
            )
        out.append(catalog[name])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", required=True, help="Gateway base URL")
    parser.add_argument("--api-key", required=True, help="SDK key (Bearer)")
    parser.add_argument(
        "--tier", nargs="+", default=["mock-low", "mock-mid", "mock-high"],
        help="Tier names to run (default: mock tiers)",
    )
    parser.add_argument(
        "--tier-json", type=Path, default=None,
        help="Optional JSON file with custom TierConfig list",
    )
    parser.add_argument(
        "--testset", type=Path,
        default=Path(__file__).parent / "translation_testset.json",
        help="Path to test set JSON",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark_results.csv"),
        help="CSV output path",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Run only the first N test cases (0 = all)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Concurrent requests per tier",
    )
    parser.add_argument(
        "--timeout-s", type=float, default=60.0,
        help="Per-request timeout",
    )
    parser.add_argument(
        "--skip-comet", action="store_true",
        help="Skip COMET (faster iteration on plumbing)",
    )
    parser.add_argument(
        "--skip-chrf", action="store_true",
        help="Skip chrF",
    )
    args = parser.parse_args()

    tiers = parse_tiers(args.tier, args.tier_json)
    cases = load_testset(args.testset)
    if args.limit > 0:
        cases = cases[: args.limit]

    print(
        f"Benchmark: {len(tiers)} tiers × {len(cases)} cases = "
        f"{len(tiers) * len(cases)} requests",
        file=sys.stderr,
    )

    results = asyncio.run(
        run_benchmark(
            endpoint=args.endpoint,
            api_key=args.api_key,
            tiers=tiers,
            cases=cases,
            concurrency=args.concurrency,
            timeout_s=args.timeout_s,
        )
    )

    if not args.skip_chrf:
        compute_chrf(results)
    if not args.skip_comet:
        compute_comet(results)

    write_csv(results, args.output)
    print(f"\nCSV written: {args.output}", file=sys.stderr)
    print_summary(results)


if __name__ == "__main__":
    main()
