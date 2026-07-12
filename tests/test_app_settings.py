"""Settings persistence for the desktop app.

The point of saving these is that a second run should be one click: reopen the
app, press 开始整理. So a corrupt or partial file must degrade to defaults rather
than block startup, and the advanced choices must actually reach the pipeline.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from settings import EFFORTS, MODELS, Settings  # noqa: E402


def test_settings_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        cfg = Settings.load(path)
        cfg.input_dir = "/papers"
        cfg.output_dir = "/out"
        cfg.score_model = "deepseek-v4-pro"
        cfg.score_effort = "high"
        cfg.workers = 8
        cfg.review_select = True
        cfg.seen_intro = True
        cfg.save()

        again = Settings.load(path)
        assert again.input_dir == "/papers"
        assert again.output_dir == "/out"
        assert again.score_model == "deepseek-v4-pro"
        assert again.score_effort == "high"
        assert again.workers == 8
        assert again.review_select is True
        assert again.seen_intro is True


def test_corrupt_settings_file_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text("{ this is not json")
        cfg = Settings.load(path)
        assert cfg.mode == "basic"
        assert cfg.seen_intro is False


def test_unknown_keys_from_a_newer_version_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({"workers": 4, "some_future_option": True}))
        cfg = Settings.load(path)
        assert cfg.workers == 4


def test_intro_shows_once_then_never_again():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        assert Settings.load(path).seen_intro is False
        cfg = Settings.load(path)
        cfg.seen_intro = True
        cfg.save()
        assert Settings.load(path).seen_intro is True


def test_offered_models_and_efforts_match_the_pipeline():
    # The dropdowns must not offer a model or effort the CLI would reject.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pipe_for_choices", ROOT / "scripts" / "gaokao_english_docx_pipeline.py"
    )
    pipe = importlib.util.module_from_spec(spec)
    sys.modules["pipe_for_choices"] = pipe
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(pipe)

    for effort in EFFORTS:
        args = pipe.parse_args(["input_docx", "--score-reasoning-effort", effort])
        assert args.score_reasoning_effort == effort

    for model in MODELS:
        args = pipe.parse_args(["input_docx", "--score-model", model])
        assert args.score_model == model
