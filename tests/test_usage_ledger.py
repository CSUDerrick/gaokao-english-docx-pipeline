"""Token counting and the cost ledger — the two things that quote a teacher a price.

Both used to lie. Tokens were estimated as ``len(text) // 3``, which is ~40% low on
Chinese and ~40% high on English. Cost was reconstructed by grepping the saved
markdown transcripts for the model name, so with 保留中间产物 off every run was priced
as pro — a flash run quoted at roughly 3x its real cost — and segment/review-select
were never counted at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deepseek_tokens  # noqa: E402
import gaokao_english_docx_pipeline as pipeline  # noqa: E402
import usage_report  # noqa: E402


def test_the_bundled_tokenizer_beats_the_character_estimate_in_both_directions():
    if not deepseek_tokens.is_exact():
        return  # the wheel is optional; the fallback is covered below

    chinese = "这是一段中文测试文本，用来看看分词器的表现。"
    english = "The quick brown fox jumps over the lazy dog and keeps running."

    # Chinese packs more tokens per character than the old //3 guess assumed...
    assert deepseek_tokens.count(chinese) > len(chinese) // 3
    # ...and English packs fewer.
    assert deepseek_tokens.count(english) < len(english) // 3


def test_a_missing_tokenizer_degrades_instead_of_crashing():
    # The count feeds a *cost display*. A packaging slip that loses the 7.5 MB vocab
    # must make the number rougher, never stop the run.
    deepseek_tokens._tokenizer.cache_clear()
    real = deepseek_tokens.tokenizer_path
    try:
        deepseek_tokens.tokenizer_path = lambda: Path("/nonexistent/tokenizer.json")
        deepseek_tokens._tokenizer.cache_clear()
        assert deepseek_tokens.is_exact() is False
        assert deepseek_tokens.count("回退估算也要给出一个正数") > 0
        assert deepseek_tokens.count("") == 0
    finally:
        deepseek_tokens.tokenizer_path = real
        deepseek_tokens._tokenizer.cache_clear()


def _args(out: Path) -> argparse.Namespace:
    return argparse.Namespace(out=str(out))


def _result(prompt: int, hit: int, completion: int) -> pipeline.ChatResult:
    return pipeline.ChatResult(
        content="{}",
        usage={
            "prompt_tokens": prompt,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": prompt - hit,
            "completion_tokens": completion,
        },
        client_used="http",
    )


def test_every_stage_lands_in_the_ledger_including_the_ones_with_no_output_dir():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for stage, model in (
            ("segment", "deepseek-v4-flash"),
            ("score", "deepseek-v4-flash"),
            ("review_select", "deepseek-v4-pro"),  # writes no per-item dir at all
            ("explain", "deepseek-v4-flash"),
        ):
            pipeline.record_usage(
                _args(out), stage=stage, item_id="x", model=model,
                thinking="enabled", effort="medium",
                result=_result(1000, 800, 500), seconds=1.5,
            )

        entries = usage_report.read_ledger(out)
        assert len(entries) == 4
        assert {e["stage"] for e in entries} == {"segment", "score", "review_select", "explain"}
        assert entries[0]["seconds"] == 1.5

        totals = usage_report.collect(out)
        assert ("review_select", "deepseek-v4-pro") in totals, "复核选题的钱以前根本没算"
        assert usage_report.grand_total(totals).calls == 4


def test_two_models_in_one_run_are_priced_separately():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        # The exact case the old report collapsed: 进阶模式 lets you score on flash and
        # explain on pro, and the two used to be summed into one stage row and priced
        # off whichever model the first transcript happened to name.
        pipeline.record_usage(
            _args(out), stage="score", item_id="a", model="deepseek-v4-flash",
            thinking="enabled", effort="medium", result=_result(10_000, 0, 1_000), seconds=1,
        )
        pipeline.record_usage(
            _args(out), stage="explain", item_id="a", model="deepseek-v4-pro",
            thinking="enabled", effort="high", result=_result(10_000, 0, 1_000), seconds=1,
        )

        totals = usage_report.collect(out)
        flash = totals[("score", "deepseek-v4-flash")]
        pro = totals[("explain", "deepseek-v4-pro")]
        assert pro.cost_usd > flash.cost_usd * 2, "pro must not be billed at flash rates, or vice versa"

        text = usage_report.report(out)
        assert "deepseek-v4-flash" in text and "deepseek-v4-pro" in text


def test_cache_hits_actually_reduce_the_quoted_cost():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        pipeline.record_usage(
            _args(out), stage="explain", item_id="a", model="deepseek-v4-pro",
            thinking="enabled", effort="high", result=_result(10_000, 9_000, 1_000), seconds=1,
        )
        cached = usage_report.grand_total(usage_report.collect(out))
        assert abs(cached.hit_rate - 0.9) < 1e-9

        fresh = usage_report.price(0, 10_000, 1_000, "deepseek-v4-pro")
        assert cached.cost_usd < fresh, "a cached pro token is ~120x cheaper; the report must show that"


def test_a_run_made_before_the_ledger_existed_still_reports():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "scores").mkdir()
        (out / "scores" / "a.json").write_text(json.dumps({
            "model": "deepseek-v4-flash",
            "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 500,
                      "prompt_cache_miss_tokens": 500, "completion_tokens": 200},
        }), encoding="utf-8")

        totals = usage_report.collect(out)
        assert totals[("score", "deepseek-v4-flash")].calls == 1
