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
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx


# Status taxonomy. The R3 fix splits "had output" from "model produced
# nothing" so empty completions / refusals don't silently inflate COMET
# averages by being excluded from scoring while still counted as
# successes.
ResultStatus = Literal["ok", "empty", "error"]


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
#
# R3 fix (#9): drop the ambiguous "zh" entry. Per BCP-47, bare "zh" is
# usually Simplified Chinese — mapping it to "Traditional Chinese" would
# silently mis-instruct the model for any future Simplified test case.
# Force the test set to use explicit "zh-TW" / "zh-Hans" tags; raw "zh"
# now falls through to the literal code so the mis-labelling is visible.
LANG_NAMES: dict[str, str] = {
    "zh-TW": "Traditional Chinese",
    "zh-Hant": "Traditional Chinese",
    "zh-Hans": "Simplified Chinese",
    "zh-CN": "Simplified Chinese",
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
    # R3 fix (#3): ttft_ms is None when the stream never produced a
    # content delta. Falling back to total_ms here polluted the percentile
    # stats with bogus "fast" TTFTs for refused / empty completions.
    ttft_ms: float | None
    total_ms: float
    # R3 fix (#5): explicit status so empty completions / refusals don't
    # silently sit in the "success but no COMET" bucket. Summary breaks
    # these out instead of conflating with errors.
    status: ResultStatus = "ok"
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
    # R3 fix (#1): the ground-truth reference is benchmark-internal — it
    # MUST NOT leak to real upstreams (some proxies log headers, and a
    # tier that echoes the header would COMET=1.0 by cheating). Only the
    # mock_ems_server reads this header to fake quality-differentiated
    # outputs; gate the send to mock-* model ids so real benchmarks never
    # leak.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if tier.model.startswith("mock-"):
        ref_b64 = base64.b64encode(case.ref_text.encode("utf-8")).decode("ascii")
        headers["X-Benchmark-Ref-Text-B64"] = ref_b64

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
                if not raw:
                    continue
                # R3 fix (#12): accept both "data: foo" (with space) and
                # "data:foo" (no space) — SSE spec permits both, and some
                # proxies normalize to the no-space form. The previous
                # strict check silently dropped every chunk on such an
                # upstream.
                if not raw.startswith("data:"):
                    continue
                tail = raw[5:].lstrip()
                if tail == "[DONE]":
                    break
                try:
                    chunk = json.loads(tail)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(chunk, dict):
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
                # R3 fix (#4): a buggy proxy can emit choices=[null] or
                # choices=["text"]. Guard the dict-shape assumption so a
                # single bad chunk doesn't escape as AttributeError, kill
                # the as_completed loop, and leak in-flight tasks.
                if not isinstance(choices[0], dict):
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
    output = "".join(output_parts).strip()
    # R3 fix (#5): empty output (refusal, content-filter, broken stream)
    # is a *category* between "ok" and "error". Classifying it makes the
    # summary honest — these cases neither score nor count as wins.
    status: ResultStatus = "ok" if output else "empty"
    # R3 fix (#3): preserve None TTFT when the stream produced no content
    # delta — the percentile math must exclude these rather than treat
    # them as fast first-tokens.
    return TranslationResult(
        tier_name=tier.name,
        case_id=case.id,
        src_lang=case.src_lang,
        ref_lang=case.ref_lang,
        category=case.category,
        src_text=case.src_text,
        ref_text=case.ref_text,
        output=output,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        status=status,
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
        ttft_ms=None,
        total_ms=(time.perf_counter() - t0) * 1000,
        status="error",
        error=error,
    )


# --------------------------------------------------------------------------- #
# Quality scoring
# --------------------------------------------------------------------------- #


def compute_chrf(results: list[TranslationResult]) -> None:
    """Annotate each result with chrF score in place. Uses sacrebleu.

    Sentence-level chrF is noisier than corpus-level, but the per-tier
    averages we report are aggregated across the SAME test set so the
    sentence/corpus difference cancels out for tier-vs-tier comparison.
    """
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
        # R3 fix (#5): only score truly "ok" rows. Empty completions and
        # errors are excluded from quality metrics by design.
        if r.status != "ok" or not r.output:
            continue
        try:
            r.chrf = metric.sentence_score(r.output, [r.ref_text]).score
        except Exception as e:  # noqa: BLE001
            print(f"chrF failed for {r.case_id}: {e}", file=sys.stderr)


def compute_comet(results: list[TranslationResult], *, batch_size: int = 8) -> None:
    """Annotate each result with COMET score in place. Heavy; needs unbabel-comet.

    R3 fix (#7): the whole scoring block is wrapped in try/except so a
    failure mid-way (network drop on model download, CUDA OOM, length
    mismatch from COMET silently dropping rows) only loses the COMET
    annotations — translations + chrF + latency stats still get written
    to CSV. Previously a 10-minute inference run could be wasted if the
    2GB model download timed out at byte 1.8GB.

    R3 fix (#6): drop ``strict=True`` from the zip — if predict returns
    fewer scores than inputs (rare but documented for COMET edge cases),
    score what we got and warn. Better than nuking the whole annotation.
    """
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError:
        print(
            "COMET skipped — install with `pip install unbabel-comet` to enable.",
            file=sys.stderr,
        )
        return

    scorable = [r for r in results if r.status == "ok" and r.output]
    if not scorable:
        print("No scorable results for COMET.", file=sys.stderr)
        return

    try:
        print(
            "Loading COMET model wmt22-comet-da (~2GB on first run)…",
            file=sys.stderr,
        )
        model_path = download_model("Unbabel/wmt22-comet-da")
        model = load_from_checkpoint(model_path)
        payload = [
            {"src": r.src_text, "mt": r.output, "ref": r.ref_text} for r in scorable
        ]
        print(
            f"Scoring {len(payload)} translations with COMET…", file=sys.stderr
        )
        output = model.predict(payload, batch_size=batch_size, progress_bar=False)
        scores = output.scores if hasattr(output, "scores") else output["scores"]
        if len(scores) != len(scorable):
            print(
                f"⚠ COMET returned {len(scores)} scores for {len(scorable)} inputs — "
                "annotating what we can.",
                file=sys.stderr,
            )
        # zip stops at the shorter sequence — no strict=True so a mismatch
        # degrades gracefully.
        for r, s in zip(scorable, scores):
            r.comet = float(s)
    except Exception as e:  # noqa: BLE001 — COMET surfaces many varieties
        print(
            f"⚠ COMET scoring failed ({type(e).__name__}: {e}). "
            "Translations + chrF + latency still written to CSV.",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def write_csv(results: list[TranslationResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "tier_name", "case_id", "status", "category", "src_lang", "ref_lang",
        "ttft_ms", "total_ms", "completion_tokens",
        "comet", "chrf", "error",
        "src_text", "ref_text", "output",
    ]
    # R3 fix (#14): utf-8-sig writes a BOM so Excel on Windows auto-
    # detects UTF-8 and renders CJK columns correctly when an operator
    # double-clicks the file. pandas / awk / every other UTF-8 reader
    # handles the BOM transparently.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))


def print_summary(results: list[TranslationResult]) -> None:
    """R3 fix (#5): break out 'empty' from 'ok' and 'error' so a tier
    that returns refusals doesn't show as 'all successful' just because
    error stayed blank.

    R3 fix (#3): TTFT percentiles are computed only over rows where TTFT
    was actually captured (status='ok' AND ttft_ms is not None). Empty
    completions don't pollute the latency distribution.
    """
    by_tier: dict[str, list[TranslationResult]] = defaultdict(list)
    for r in results:
        by_tier[r.tier_name].append(r)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for tier_name in sorted(by_tier):
        rows = by_tier[tier_name]
        ok = [r for r in rows if r.status == "ok"]
        empties = [r for r in rows if r.status == "empty"]
        failures = [r for r in rows if r.status == "error"]
        comet_scores = [r.comet for r in ok if r.comet is not None]
        chrf_scores = [r.chrf for r in ok if r.chrf is not None]
        ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
        totals = [r.total_ms for r in ok]

        print(f"\nTIER: {tier_name}")
        print("-" * 70)
        print(
            f"  cases:           {len(rows)} "
            f"(ok={len(ok)}, empty={len(empties)}, error={len(failures)})"
        )
        if comet_scores:
            print(
                f"  COMET (avg):     {statistics.mean(comet_scores):.3f} "
                f"(n={len(comet_scores)})"
            )
        if chrf_scores:
            print(
                f"  chrF  (avg):     {statistics.mean(chrf_scores):.2f} "
                f"(n={len(chrf_scores)})"
            )
        if ttfts:
            print(
                f"  TTFT  ms p50/p99: {_pct(ttfts, 50):.0f} / {_pct(ttfts, 99):.0f} "
                f"(n={len(ttfts)})"
            )
        if totals:
            print(
                f"  total ms p50/p99: {_pct(totals, 50):.0f} / {_pct(totals, 99):.0f} "
                f"(n={len(totals)})"
            )

        # Per-category breakdown — only on truly scorable rows.
        by_cat: dict[str, list[TranslationResult]] = defaultdict(list)
        for r in ok:
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

        if empties:
            print(f"  empties ({len(empties)}):")
            for r in empties[:3]:
                print(f"    {r.case_id} ({r.category})")
            if len(empties) > 3:
                print(f"    … and {len(empties) - 3} more")
        if failures:
            print(f"  failures ({len(failures)}):")
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
    # R3 fix (#8): accept --api-key OR GATEWAY_API_KEY env. Env var is
    # preferred — putting the SDK key on the command line leaks it into
    # `ps`, shell history, and CI log captures on every invocation.
    parser.add_argument(
        "--api-key", default=os.environ.get("GATEWAY_API_KEY"),
        help=(
            "SDK key (Bearer). Defaults to $GATEWAY_API_KEY. "
            "Prefer the env var on shared machines — CLI args show in `ps`."
        ),
    )
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

    if not args.api_key:
        parser.error(
            "missing API key — pass --api-key or set GATEWAY_API_KEY env var"
        )

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

    # R3 fix (#7): write the raw CSV BEFORE scoring so an expensive run
    # isn't lost when COMET fails 10 minutes into model download. chrF
    # is cheap so it can stay before, but the COMET pass updates the
    # CSV in place.
    if not args.skip_chrf:
        compute_chrf(results)
    write_csv(results, args.output)
    print(f"\nCSV (pre-COMET) written: {args.output}", file=sys.stderr)

    if not args.skip_comet:
        compute_comet(results)
        # Re-write with COMET scores merged.
        write_csv(results, args.output)
        print(f"CSV (with COMET) written: {args.output}", file=sys.stderr)

    print_summary(results)


if __name__ == "__main__":
    main()
