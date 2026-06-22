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
    EXPECTED_COLUMNS = (
        "tier_name", "case_id", "status", "category", "src_lang", "ref_lang",
        "ttft_ms", "total_ms", "completion_tokens",
        "comet", "chrf", "error",
        "src_text", "ref_text", "output",
    )

    def test_csv_columns_exact_set_and_order(self, tmp_path: Path) -> None:
        """R3 fix: pin both the column SET and ORDER so a refactor that
        reorders fields gets caught — downstream consumers (pandas pivot,
        awk, BI tools) may index positionally."""
        out = tmp_path / "out.csv"
        tb.write_csv([], out)
        with out.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames is not None
            assert tuple(reader.fieldnames) == self.EXPECTED_COLUMNS

    def test_csv_round_trip_with_bom_for_excel(self, tmp_path: Path) -> None:
        """R3 fix #14: CSV must carry a UTF-8 BOM so Excel on Windows
        renders CJK columns correctly; pandas / DictReader handle the BOM
        transparently with encoding='utf-8-sig'.
        """
        results = [
            tb.TranslationResult(
                tier_name="mid", case_id="x1",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="hello, world", ref_text="嗨，你好", output="嗨，你好",
                ttft_ms=150.0, total_ms=320.0,
                status="ok", completion_tokens=1, comet=0.85, chrf=72.4,
            ),
        ]
        out = tmp_path / "out.csv"
        tb.write_csv(results, out)

        # Raw bytes start with the UTF-8 BOM (EF BB BF).
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "CSV missing UTF-8 BOM"

        # csv.DictReader with utf-8-sig handles the BOM transparently.
        with out.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] == "ok"
        assert r["src_text"] == "hello, world"   # comma round-trips OK
        assert r["ref_text"] == "嗨，你好"        # CJK round-trips OK
        assert r["comet"] == "0.85"

    def test_csv_handles_embedded_commas_quotes_newlines(self, tmp_path: Path) -> None:
        """R3 fix #14: error messages and translations contain commas,
        quotes, and newlines. csv.DictWriter handles RFC 4180 escaping;
        verify the round-trip explicitly so a future hand-rolled emitter
        can't silently break it.
        """
        results = [
            tb.TranslationResult(
                tier_name="t", case_id="x",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text='He said, "hi"\nand left.', ref_text="他說「嗨」，然後離開了。",
                output="他說「嗨」，然後離開了。",
                ttft_ms=10.0, total_ms=20.0,
                status="ok", error='HTTP 422: {"err":"bad"}\n',
            ),
        ]
        out = tmp_path / "out.csv"
        tb.write_csv(results, out)
        with out.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["src_text"] == 'He said, "hi"\nand left.'
        assert rows[0]["error"] == 'HTTP 422: {"err":"bad"}\n'

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
        # R3 fix #1: mock-prefixed model name → ref header is sent.
        tier = tb.TierConfig(name="t", model="mock-mid")

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
        assert result.status == "ok"
        assert result.output == "你好"
        assert result.completion_tokens == 2
        assert result.ttft_ms is not None and result.ttft_ms > 0
        assert result.total_ms >= result.ttft_ms

    @pytest.mark.asyncio
    async def test_ref_header_NOT_sent_to_non_mock_tier(self) -> None:
        """R3 fix #1: real upstreams must NEVER receive the ref-text header
        — leak risk + cheating-tier vector."""
        case = tb.TestCase(
            id="t-real", src_lang="en", src_text="hello",
            ref_lang="zh-TW", ref_text="你好", category="general",
        )
        # Real-tier model name (no 'mock-' prefix).
        tier = tb.TierConfig(name="real-low", model="qwen-2.5-3b-int4")

        sent_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent_headers.update(
                {k.lower(): v for k, v in request.headers.items()}
            )
            return _sse_response([
                json.dumps({"choices": [{"index": 0, "delta": {"content": "x"}}]}),
            ])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await tb.translate_one(
                client=client, endpoint="http://upstream", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )

        assert "x-benchmark-ref-text-b64" not in sent_headers, (
            "ref text leaked to non-mock upstream — see R3 fix #1"
        )
        # Auth header still goes through.
        assert sent_headers["authorization"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_empty_completion_classified_as_empty_not_ok(self) -> None:
        """R3 fix #5: stream with zero content deltas → status='empty',
        excluded from chrF/COMET and counted separately in summary so it
        doesn't silently inflate quality averages."""
        case = tb.TestCase(
            id="t-empty", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="mock-mid")

        def handler(_: httpx.Request) -> httpx.Response:
            return _sse_response([
                # No content deltas at all — only the terminal usage chunk.
                json.dumps({
                    "choices": [],
                    "usage": {"completion_tokens": 0},
                }),
            ])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )

        assert result.status == "empty"
        assert result.output == ""
        assert result.error == ""
        # R3 fix #3: TTFT stays None when no content arrived, NOT total_ms.
        assert result.ttft_ms is None

    @pytest.mark.asyncio
    async def test_non_dict_choices_does_not_crash(self) -> None:
        """R3 fix #4: buggy upstream emits choices=[null] or choices=['s'];
        the parser must skip, not raise AttributeError that leaks tasks."""
        case = tb.TestCase(
            id="t-bad", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="mock-mid")

        def handler(_: httpx.Request) -> httpx.Response:
            return _sse_response([
                json.dumps({"choices": [None]}),         # null choice
                json.dumps({"choices": ["a string"]}),   # non-dict choice
                json.dumps({"choices": [{"delta": {"content": "ok"}}]}),  # real
                json.dumps({"choices": [], "usage": {"completion_tokens": 1}}),
            ])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )

        # Bad chunks silently skipped, real chunk captured.
        assert result.status == "ok"
        assert result.output == "ok"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_data_prefix_without_space_is_accepted(self) -> None:
        """R3 fix #12: SSE spec permits 'data:foo' (no space). Some proxies
        normalize to that form; the parser must accept both."""
        case = tb.TestCase(
            id="t-nospace", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="嗨", category="general",
        )
        tier = tb.TierConfig(name="t", model="mock-mid")

        def handler(_: httpx.Request) -> httpx.Response:
            # Manually build SSE WITHOUT the space after 'data:'.
            body = (
                'data:{"choices":[{"delta":{"content":"hi"}}]}\n\n'
                'data:[DONE]\n\n'
            )
            return httpx.Response(
                200, content=body.encode(),
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await tb.translate_one(
                client=client, endpoint="http://mock", api_key="k",
                tier=tier, case=case, timeout_s=5.0,
            )

        assert result.status == "ok"
        assert result.output == "hi"

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

    def test_breaks_out_empty_and_error_status(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """R3 fix #5: empty completions and errors get their own counts
        in 'cases: ok=X, empty=Y, error=Z' so a tier returning refusals
        doesn't look like 'all successful'.
        """
        results = [
            tb.TranslationResult(
                tier_name="t", case_id="a",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="hi", ref_text="嗨", output="嗨",
                ttft_ms=50.0, total_ms=120.0, status="ok",
            ),
            tb.TranslationResult(
                tier_name="t", case_id="b",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="x", ref_text="嗨", output="",
                ttft_ms=None, total_ms=80.0, status="empty",
            ),
            tb.TranslationResult(
                tier_name="t", case_id="c",
                src_lang="en", ref_lang="zh-TW", category="general",
                src_text="y", ref_text="嗨", output="",
                ttft_ms=None, total_ms=200.0, status="error", error="HTTP 502",
            ),
        ]
        tb.print_summary(results)
        out = capsys.readouterr().out
        assert "ok=1" in out
        assert "empty=1" in out
        assert "error=1" in out
        # Failures section also shows the error reason.
        assert "HTTP 502" in out


# --------------------------------------------------------------------------- #
# Mock server quality-gradient regression (R3 #2)
# --------------------------------------------------------------------------- #


class TestMockServerCJK:
    """R3 fix #2: mock_low must produce a CLEARLY shorter / different
    output than the reference for CJK input — the bundled testset is mostly
    zh-TW, and a tier-equality bug here defeats the demo's whole purpose.
    """

    def test_mock_low_truncates_cjk_reference(self) -> None:
        # Import lazily so the test file works even if mock_ems_server
        # imports something heavy that fails on this host.
        sys.path.insert(0, str(SCRIPTS_DIR))
        import mock_ems_server as mock

        ref = "今天天氣不錯，要不要去散步？"
        out = mock._mangle_low(ref, "It's a nice day, want to walk?")
        # Output must be strictly shorter and end with the truncation marker.
        assert out != ref, "mock-low returned the reference verbatim — CJK bug"
        assert len(out) < len(ref) + 5  # truncated, not appended
        assert "..." in out

    def test_mock_mid_alters_cjk_reference(self) -> None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import mock_ems_server as mock

        ref = "今天天氣不錯，要不要去散步？"
        out = mock._mangle_mid(ref, "x")
        assert out != ref, "mock-mid returned the reference verbatim — CJK bug"
        # Should still recognizably overlap (mid is degraded, not garbage).
        assert any(c in out for c in ref[:5])


# --------------------------------------------------------------------------- #
# Integration test — actually start the mock server in a subprocess
# (R3 fix #13: MockTransport tests miss network framing / SSE chunking /
# real TTFT delays. Spin up uvicorn and exercise the full HTTP path.)
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_server():
    """Start scripts/mock_ems_server.py in a subprocess, yield its URL.

    Uses a free port + retry-on-startup so multiple test runs don't
    conflict. Skipped if the host lacks subprocess capability (CI sandbox).
    """
    import socket
    import subprocess

    # Find a free port.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS_DIR / "mock_ems_server.py"),
            "--port", str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for /health to come up — bounded ~3s.
    import time as _time
    url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            r = httpx.get(f"{url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        _time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("mock server did not start within 3s")

    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


class TestMockServerIntegration:
    @pytest.mark.asyncio
    async def test_ttft_is_separately_observable_from_total(
        self, mock_server: str
    ) -> None:
        """R3 fix #13: a regression that broke TTFT capture (e.g. moved
        the ttft_ms assignment after the loop) would still pass the
        MockTransport tests because they deliver chunks synchronously.
        The real subprocess server applies asyncio.sleep delays between
        chunks — verify TTFT < total by a measurable margin."""
        case = tb.TestCase(
            id="int-1", src_lang="en", src_text="hello",
            ref_lang="zh-TW", ref_text="你好世界這是一個比較長的參考文字以便切片",
            category="general",
        )
        tier = tb.TierConfig(name="mock-high", model="mock-high", max_tokens=128)

        async with httpx.AsyncClient() as client:
            result = await tb.translate_one(
                client=client, endpoint=mock_server, api_key="k",
                tier=tier, case=case, timeout_s=10.0,
            )

        assert result.status == "ok"
        assert result.output  # non-empty
        assert result.ttft_ms is not None
        # The mock applies a 300ms TTFT delay + 20ms per token to mock-high.
        # With a multi-char reference, total should exceed TTFT by ≥ 50ms.
        assert result.total_ms - result.ttft_ms > 50, (
            f"TTFT and total too close ({result.ttft_ms:.0f}ms vs "
            f"{result.total_ms:.0f}ms) — TTFT capture may be broken"
        )

    @pytest.mark.asyncio
    async def test_mock_tiers_produce_distinct_quality_outputs(
        self, mock_server: str
    ) -> None:
        """R3 fix #2 + #13: across the three mock tiers the SAME CJK
        reference should produce three DIFFERENT outputs — that's the
        whole point of the mock. If mock-low and mock-high are equal,
        the bug is back."""
        case = tb.TestCase(
            id="int-2", src_lang="en", src_text="hi",
            ref_lang="zh-TW", ref_text="今天天氣不錯要不要去散步",
            category="general",
        )
        outputs: dict[str, str] = {}
        async with httpx.AsyncClient() as client:
            for tier_name in ("mock-low", "mock-mid", "mock-high"):
                tier = tb.TierConfig(name=tier_name, model=tier_name)
                r = await tb.translate_one(
                    client=client, endpoint=mock_server, api_key="k",
                    tier=tier, case=case, timeout_s=10.0,
                )
                outputs[tier_name] = r.output

        # mock-high returns the reference verbatim; mock-low/mid mangle it.
        assert outputs["mock-high"] == case.ref_text
        assert outputs["mock-low"] != outputs["mock-high"], (
            "mock-low matches reference — CJK whitespace-split bug returned"
        )
        assert outputs["mock-mid"] != outputs["mock-high"]
        # mock-low should be visibly truncated.
        assert len(outputs["mock-low"]) < len(outputs["mock-high"])
