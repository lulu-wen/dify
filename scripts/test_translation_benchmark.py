"""Unit tests for translation_benchmark.py against the mock EMS.

Run with: `uv run pytest scripts/test_translation_benchmark.py -v`

Tests run in two layers:

* **Pure function tests** — prompt building, testset loading, CSV writing.
  No network, no subprocess.
* **Mock-EMS integration tests** — translate_one() against an in-process
  httpx.MockTransport that mimics the mock_ems_server SSE stream. Verifies
  TTFT extraction, output accumulation, error envelope handling, and
  stream-options usage-chunk parsing.
"""

from __future__ import annotations

import base64
import csv
import json
from pathlib import Path

import httpx
import pytest

# Local import (script lives at scripts/)
import sys

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import translation_benchmark as tb  # noqa: E402


TESTSET_PATH = SCRIPTS_DIR / "translation_testset.json"


# --------------------------------------------------------------------------- #
# Pure function tests
# --------------------------------------------------------------------------- #


class TestPromptBuilding:
    def test_zh_TW_target_uses_traditional_chinese_label(self) -> None:
        case = tb.TestCase(
            id="x", src_lang="en", src_text="hello",
            ref_lang="zh-TW", ref_text="你好", category="general",
        )
        prompt = tb.build_translation_prompt(case)
        assert "Traditional Chinese" in prompt
        assert "hello" in prompt
        assert "Output ONLY the translation" in prompt

    def test_unknown_target_lang_falls_back_to_raw_code(self) -> None:
        case = tb.TestCase(
            id="x", src_lang="en", src_text="hi",
            ref_lang="xx-YZ", ref_text="???", category="general",
        )
        prompt = tb.build_translation_prompt(case)
        assert "xx-YZ" in prompt


class TestTestsetLoader:
    def test_loads_bundled_testset(self) -> None:
        cases = tb.load_testset(TESTSET_PATH)
        assert len(cases) >= 20, "test set too small to be useful"

        categories = {c.category for c in cases}
        assert categories == {"general", "base_station", "legal"}, (
            f"expected exactly 3 categories, got {categories}"
        )

        # Every entry has non-empty source + reference (would-be ground
        # truth bug if either is blank).
        for c in cases:
            assert c.src_text.strip(), f"{c.id} has empty src_text"
            assert c.ref_text.strip(), f"{c.id} has empty ref_text"

    def test_ids_are_unique(self) -> None:
        cases = tb.load_testset(TESTSET_PATH)
        ids = [c.id for c in cases]
        assert len(ids) == len(set(ids)), "duplicate case ids"


class TestParseTiers:
    def test_builtin_tiers_resolve(self) -> None:
        tiers = tb.parse_tiers(["mock-low", "mid"], custom_path=None)
        assert [t.name for t in tiers] == ["mock-low", "mid"]

    def test_unknown_tier_errors(self) -> None:
        with pytest.raises(SystemExit, match="unknown tier"):
            tb.parse_tiers(["does-not-exist"], custom_path=None)

    def test_custom_tier_json_extends_catalog(self, tmp_path: Path) -> None:
        custom = tmp_path / "tiers.json"
        custom.write_text(json.dumps([
            {"name": "experimental", "model": "test-7b", "max_tokens": 256}
        ]))
        tiers = tb.parse_tiers(["experimental"], custom_path=custom)
        assert len(tiers) == 1
        assert tiers[0].name == "experimental"
        assert tiers[0].max_tokens == 256


class TestCsvWriter:
    def test_csv_has_all_expected_columns(self, tmp_path: Path) -> None:
        results = [
            tb.TranslationResult(
                tier_name="mid", case_id="x1",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="hi", ref_text="嗨", output="嗨",
                ttft_ms=150.0, total_ms=320.0,
                completion_tokens=1, comet=0.85, chrf=72.4,
            ),
        ]
        out = tmp_path / "out.csv"
        tb.write_csv(results, out)

        with out.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        r = rows[0]
        # Spot-check critical columns the dashboard will read.
        assert r["tier_name"] == "mid"
        assert r["case_id"] == "x1"
        assert r["category"] == "general"
        assert r["comet"] == "0.85"
        assert r["ttft_ms"] == "150.0"
        assert r["error"] == ""

    def test_csv_writer_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deeply" / "nested" / "out.csv"
        tb.write_csv([], nested)
        assert nested.exists()


class TestPercentile:
    def test_p50_is_median(self) -> None:
        assert tb._pct([10.0, 20.0, 30.0], 50) == pytest.approx(20.0)

    def test_p99_of_single_value(self) -> None:
        assert tb._pct([42.0], 99) == 42.0

    def test_empty_returns_zero(self) -> None:
        assert tb._pct([], 50) == 0.0


# --------------------------------------------------------------------------- #
# Mock-EMS integration tests via httpx.MockTransport
# --------------------------------------------------------------------------- #


def _sse_response(chunks: list[str]) -> httpx.Response:
    """Build a 200 SSE response from raw 'data: ...' lines."""
    body = "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        content=body.encode(),
        headers={"content-type": "text/event-stream"},
    )


class TestTranslateOne:
    @pytest.mark.asyncio
    async def test_happy_path_records_ttft_and_output(self) -> None:
        case = tb.TestCase(
            id="t1", src_lang="en", src_text="hello",
            ref_lang="zh-TW", ref_text="你好", category="general",
        )
        tier = tb.TierConfig(name="t", model="m1")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            body = json.loads(request.content)
            assert body["stream"] is True
            assert body["stream_options"] == {"include_usage": True}
            # Reference header gets forwarded for mock-EMS fake-quality
            # (base64-encoded so httpx accepts CJK refs).
            decoded = base64.b64decode(
                request.headers["x-benchmark-ref-text-b64"]
            ).decode("utf-8")
            assert decoded == "你好"
            return _sse_response([
                json.dumps({"choices": [{"index": 0, "delta": {"content": "你"}}]}),
                json.dumps({"choices": [{"index": 0, "delta": {"content": "好"}}]}),
                # Terminal usage chunk (empty choices + usage block).
                json.dumps({
                    "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }),
            ])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )

        assert result.error == ""
        assert result.output == "你好"
        assert result.completion_tokens == 2
        assert result.ttft_ms > 0
        assert result.total_ms >= result.ttft_ms

    @pytest.mark.asyncio
    async def test_non_2xx_recorded_as_error(self) -> None:
        case = tb.TestCase(
            id="t2", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="m1")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="overloaded")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )
        assert "HTTP 503" in result.error
        assert "overloaded" in result.error
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_transport_error_caught(self) -> None:
        case = tb.TestCase(
            id="t3", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="m1")

        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )
        assert "transport" in result.error
        assert "connection refused" in result.error

    @pytest.mark.asyncio
    async def test_non_numeric_completion_tokens_does_not_crash(self) -> None:
        """Mirrors the gateway's R2 fix — malformed upstream usage value
        should not crash the benchmark."""
        case = tb.TestCase(
            id="t4", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="m1")

        def handler(_: httpx.Request) -> httpx.Response:
            return _sse_response([
                json.dumps({"choices": [{"index": 0, "delta": {"content": "嗨"}}]}),
                json.dumps({
                    "choices": [],
                    "usage": {"completion_tokens": "NaN"},  # garbage
                }),
            ])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )
        assert result.error == ""
        assert result.output == "嗨"
        assert result.completion_tokens == 0  # degraded gracefully


class TestComputeChrf:
    def test_perfect_translation_scores_high(self) -> None:
        pytest.importorskip("sacrebleu")
        r = tb.TranslationResult(
            tier_name="t", case_id="x",
            src_lang="en", ref_lang="zh-TW", category="general",
            src_text="hello", ref_text="你好世界", output="你好世界",
            ttft_ms=100.0, total_ms=200.0,
        )
        tb.compute_chrf([r])
        assert r.chrf is not None
        assert r.chrf > 90  # essentially perfect

    def test_completely_wrong_scores_low(self) -> None:
        pytest.importorskip("sacrebleu")
        r = tb.TranslationResult(
            tier_name="t", case_id="x",
            src_lang="en", ref_lang="zh-TW", category="general",
            src_text="hello", ref_text="你好世界", output="完全不對的東西xyzabc",
            ttft_ms=100.0, total_ms=200.0,
        )
        tb.compute_chrf([r])
        # chrF on totally different output should be low (<30 typically).
        assert r.chrf is not None
        assert r.chrf < 50


class TestPrintSummary:
    def test_does_not_crash_on_empty(self, capsys: pytest.CaptureFixture) -> None:
        tb.print_summary([])
        # Should print headers without exceptions.
        out = capsys.readouterr().out
        assert "SUMMARY" in out

    def test_groups_by_tier(self, capsys: pytest.CaptureFixture) -> None:
        results = [
            tb.TranslationResult(
                tier_name="low", case_id="a",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="hi", ref_text="嗨", output="嗨",
                ttft_ms=50.0, total_ms=120.0, comet=0.7,
            ),
            tb.TranslationResult(
                tier_name="high", case_id="b",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="hi", ref_text="嗨", output="嗨",
                ttft_ms=300.0, total_ms=900.0, comet=0.95,
            ),
        ]
        tb.print_summary(results)
        out = capsys.readouterr().out
        assert "TIER: low" in out
        assert "TIER: high" in out
        assert "0.700" in out  # low tier COMET
        assert "0.950" in out  # high tier COMET
