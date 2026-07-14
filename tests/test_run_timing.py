"""The time estimate: right-ish on the first run, accurate after a few.

The old ETA weighted every stage equally, so the local sub-second segmentation and
the multi-minute explanation each counted for 1/9 of the bar. It read 11% within a
second of starting and then barely moved.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_timing as rt  # noqa: E402


def _fresh(tmp: str) -> rt.Timings:
    return rt.Timings(Path(tmp) / "timings.json")


def test_with_no_history_the_priors_still_rank_the_stages_correctly():
    with tempfile.TemporaryDirectory() as tmp:
        t = _fresh(tmp)
        segment = t.estimate("segment", units=3, model="deepseek-v4-flash", effort="medium", thinking="enabled")
        explain = t.estimate("explain", units=18, model="deepseek-v4-flash", effort="medium", thinking="enabled")
        assert explain > segment * 20, "切分是本地的，解析要几分钟——不能等权"


def test_pro_is_estimated_slower_than_flash_until_there_is_real_history():
    with tempfile.TemporaryDirectory() as tmp:
        t = _fresh(tmp)
        flash = t.estimate("explain", 18, "deepseek-v4-flash", "medium", "enabled")
        pro = t.estimate("explain", 18, "deepseek-v4-pro", "high", "enabled")
        assert pro > flash


def test_history_wins_over_the_prior_and_uses_the_median():
    with tempfile.TemporaryDirectory() as tmp:
        t = _fresh(tmp)
        # Three honest runs and one that got throttled to death.
        for seconds in (180.0, 190.0, 200.0, 3600.0):
            t.record("explain", 18, "deepseek-v4-flash", "medium", "enabled", seconds)

        estimate = t.estimate("explain", 18, "deepseek-v4-flash", "medium", "enabled")
        assert 180 <= estimate <= 210, f"the 429-throttled outlier must not poison it: {estimate}"


def test_history_is_per_configuration_not_global():
    with tempfile.TemporaryDirectory() as tmp:
        t = _fresh(tmp)
        t.record("explain", 18, "deepseek-v4-flash", "medium", "enabled", 180.0)
        # pro has no history of its own, so it must not inherit flash's number.
        assert t.estimate("explain", 18, "deepseek-v4-pro", "high", "enabled") > 180.0


def test_a_fixed_stage_does_not_scale_with_the_number_of_questions():
    with tempfile.TemporaryDirectory() as tmp:
        t = _fresh(tmp)
        one = t.estimate("export-docx", 1, "deepseek-v4-flash", "medium", "enabled")
        many = t.estimate("export-docx", 100, "deepseek-v4-flash", "medium", "enabled")
        assert one == many, "导出的耗时取决于要合并的文档，不是题数"


def test_history_round_trips_and_is_capped():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timings.json"
        t = rt.Timings(path)
        for _ in range(rt.MAX_SAMPLES + 10):
            t.record("score", 27, "deepseek-v4-flash", "medium", "enabled", 62.0)
        t.save()

        reloaded = rt.Timings(path)
        key = next(iter(reloaded.samples))
        assert len(reloaded.samples[key]) == rt.MAX_SAMPLES, "old runs must age out"
        assert abs(reloaded.estimate("score", 27, "deepseek-v4-flash", "medium", "enabled") - 62.0) < 1.0


def test_a_corrupt_history_file_does_not_block_a_run():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        t = rt.Timings(path)
        assert t.samples == {}
        assert t.estimate("score", 27, "deepseek-v4-flash", "medium", "enabled") > 0


def test_durations_read_like_a_person_wrote_them():
    assert rt.format_duration(45) == "45 秒"
    assert rt.format_duration(125) == "2 分 5 秒"
    assert rt.format_duration(3700) == "1 小时 1 分"
    assert rt.format_duration(-5) == "0 秒"
