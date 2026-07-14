"""分步重跑: redo one stage without paying for the rest — and without breaking the rest.

Three things have to hold, and each one has already gone wrong once:

* ``--init`` is a ``shutil.rmtree`` of the output folder. It was passed on *every*
  run (the GUI appended it to the segment stage unconditionally), which is why
  re-running a single stage was impossible in the first place.
* ``--force`` must land only on the stage the teacher asked to regenerate. Put it on
  the downstream stages too and every re-selection re-explains all 18 questions at
  full price instead of only the new ones.
* A job that changes *which* questions are selected must drag explain/vocab/export
  behind it, or the export ships a teacher edition missing a question's explanation
  and a word list built from the previous selection.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

import gaokao_english_docx_pipeline as pipeline  # noqa: E402
from main import Job, Worker, full_run, rerun_jobs, stage_config  # noqa: E402
from settings import Settings  # noqa: E402


def _argv(job: Job, stage: str) -> list[str]:
    return Worker(Path("in"), Path("out"), {}, Settings(), job)._argv(stage)


def test_only_a_full_run_may_wipe_the_output_directory():
    assert full_run(review_select=False).init is True
    assert "--init" in _argv(full_run(False), "segment")

    for key, job in rerun_jobs(False).items():
        if key == "resegment":
            continue  # re-segmenting *is* a full run: everything below it changes
        for stage in job.stages:
            assert "--init" not in _argv(job, stage), f"{job.name}/{stage} would delete the other stages' work"


def test_force_lands_only_on_the_stage_the_teacher_asked_to_redo():
    jobs = rerun_jobs(False)

    explain = jobs["reexplain"]
    assert "--force" in _argv(explain, "explain")
    # The downstream stages must NOT be forced: forcing a downstream API stage is how
    # a one-stage rerun quietly turns into a full bill.
    for stage in explain.stages:
        if stage != "explain":
            assert "--force" not in _argv(explain, stage)

    reselect = jobs["reselect"]
    assert reselect.force == "", "re-selecting must reuse every explanation it can"
    for stage in reselect.stages:
        assert "--force" not in _argv(reselect, stage), "only the new questions should cost money"


def test_reselecting_tells_the_model_the_current_picks_were_rejected():
    # Local selection is a deterministic ranking: re-running it returns the same
    # questions. Only the AI review can choose differently, and only if it is told.
    reselect = rerun_jobs(False)["reselect"]
    assert reselect.stages[0] == "review-select"
    assert "--reselect" in _argv(reselect, "review-select")
    assert pipeline.parse_args(["x", "--mode", "review-select", "--reselect"]).reselect is True


def test_every_job_that_changes_the_selection_carries_its_downstream():
    for key in ("resegment", "rescore", "reselect"):
        stages = rerun_jobs(False)[key].stages
        for needed in ("explain", "vocab", "export-docx"):
            assert needed in stages, f"{key} changes which questions are in play; {needed} must follow"

    # And the ones that don't change the selection must not drag the world with them.
    assert rerun_jobs(False)["revocab"].stages == ["vocab", "export-docx"]
    assert rerun_jobs(False)["reexport"].stages == ["export-docx"]
    assert "explain" not in rerun_jobs(False)["revocab"].stages, "重生词汇 must not re-pay for explanations"


def test_review_select_is_only_in_a_chain_when_it_is_switched_on():
    assert "review-select" not in rerun_jobs(review_select=False)["rescore"].stages
    assert "review-select" in rerun_jobs(review_select=True)["rescore"].stages
    # ...except for 重新选题, which *is* the AI review — that is the only way to reselect.
    assert "review-select" in rerun_jobs(review_select=False)["reselect"].stages


def test_every_stage_of_every_job_is_a_mode_the_pipeline_dispatches():
    jobs = [full_run(True), full_run(False), *rerun_jobs(True).values(), *rerun_jobs(False).values()]
    for job in jobs:
        for stage in job.stages:
            assert pipeline.parse_args(["x", "--mode", stage]).mode == stage


def test_each_stage_is_timed_under_the_model_that_actually_runs_it():
    cfg = Settings()
    cfg.score_model, cfg.explain_model = "deepseek-v4-flash", "deepseek-v4-pro"

    assert stage_config(cfg, "score")[0] == "deepseek-v4-flash"
    assert stage_config(cfg, "explain")[0] == "deepseek-v4-pro"
    # Local stages must not be filed under a model, or a flash run and a pro run
    # would keep separate (and identical) histories for the same local work.
    assert stage_config(cfg, "export-docx")[0] == "local"
    assert stage_config(cfg, "select")[0] == "local"


def test_the_interface_does_not_frame_the_tool_around_money():
    """The copy should encourage a teacher to use this, not warn her it burns cash.

    The cost read-out *after* a run stays — it is a fact she asked to see. What went is
    the framing that was scattered through the static labels ("不重复花钱", "便宜快",
    "更慢更贵", "不花钱"), which read as "careful, this is expensive" every time she
    opened the window.
    """
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    presets = (ROOT / "scripts" / "model_presets.py").read_text(encoding="utf-8")

    for text, quoted in ((source, "app/main.py"), (presets, "scripts/model_presets.py")):
        # Only the strings a teacher can read; the comments explaining the pricing model
        # are for whoever maintains this.
        labels = re.findall(r'"([^"\\]*[一-鿿][^"\\]*)"', text)
        for label in labels:
            for word in ("花钱", "便宜", "更贵", "烧钱"):
                assert word not in label, f"{quoted} 的界面文案里又出现了「{word}」：{label!r}"
