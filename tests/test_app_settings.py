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
sys.path.insert(0, str(ROOT / "scripts"))

import providers as pv  # noqa: E402
from settings import (  # noqa: E402
    CUSTOM,
    SCHEMA_VERSION,
    QUALITY,
    SPEED,
    Settings,
    efforts_for,
    model_ids_for,
    normalize_effort,
    preset_values,
)

DEEPSEEK = "deepseek"


def _pipeline():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pipe_for_settings", ROOT / "scripts" / "gaokao_english_docx_pipeline.py"
    )
    pipe = importlib.util.module_from_spec(spec)
    sys.modules["pipe_for_settings"] = pipe
    spec.loader.exec_module(pipe)
    return pipe


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


def test_the_vocab_mode_is_the_teachers_and_survives_a_schema_migration():
    """完整/困难 is a teaching choice, not a model default.

    The MODEL_KEYS migration exists to drop stale *model defaults* so a saved file cannot
    pin yesterday's models forever. A preference the teacher deliberately set must not get
    swept up in that — if it did, she would silently get the other handout after an update.
    """
    from settings import MODEL_KEYS

    assert "vocab_mode" not in MODEL_KEYS

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        assert Settings.load(path).vocab_mode == "whole", "默认：困难（整卷）"

        cfg = Settings.load(path)
        cfg.vocab_mode = "chunked"
        cfg.save()
        assert Settings.load(path).vocab_mode == "chunked"

        # An old file predating the migration still gets to keep the choice.
        path.write_text(json.dumps({"version": 2, "vocab_mode": "chunked", "workers": 8}), encoding="utf-8")
        cfg = Settings.load(path)
        assert cfg.vocab_mode == "chunked", "迁移把老师的选择弄丢了"
        assert cfg.score_model == "deepseek-v4-flash", "但过期的模型默认值还是要被丢掉"


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
    """The dropdowns must not offer a model or effort the CLI would reject.

    Checked for every provider, not just DeepSeek: an OpenAI model name reaching the
    pipeline's DeepSeek defaults is a 404 ten minutes into a run.
    """
    pipe = _pipeline()

    for provider in pv.PROVIDER_ORDER:
        for model in model_ids_for(provider):
            args = pipe.parse_args(["input_docx", "--provider", provider, "--score-model", model])
            assert args.score_model == model
            for effort in efforts_for(provider, model):
                args = pipe.parse_args([
                    "input_docx", "--provider", provider,
                    "--score-model", model, "--score-reasoning-effort", effort,
                ])
                assert args.score_reasoning_effort == effort


def test_stale_model_defaults_are_upgraded_but_choices_are_kept():
    """A saved file must not pin the models to yesterday's default forever.

    A v2 settings.json was written when every stage defaulted to deepseek-v4-pro.
    The default is now 速度优先 (flash) — a run you are willing to repeat — so that
    old file must not go on forcing pro: it holds a stale *default* masquerading as
    a preference. Everything the teacher actually chose is still kept.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({
            "version": 2,
            "input_dir": "/papers",
            "output_dir": "/out",
            "score_model": "deepseek-v4-pro",
            "enrich_model": "deepseek-v4-pro",
            "review_model": "deepseek-v4-pro",
            "workers": 8,
            "verbose": True,
            "seen_intro": True,
        }), encoding="utf-8")

        cfg = Settings.load(path)
        assert cfg.score_model == "deepseek-v4-flash"
        assert cfg.explain_model == "deepseek-v4-flash"
        assert cfg.preset == SPEED
        assert cfg.score_thinking == "enabled" and cfg.explain_thinking == "enabled"
        assert cfg.segment_model == "deepseek-v4-flash", "the fallback segmenter stays cheap"

        # ...and the teacher's own choices survive untouched
        assert cfg.input_dir == "/papers" and cfg.output_dir == "/out"
        assert cfg.workers == 8 and cfg.verbose is True and cfg.seen_intro is True

        # once migrated, an explicit choice sticks
        cfg.score_model = "deepseek-v4-pro"
        cfg.save()
        assert Settings.load(path).score_model == "deepseek-v4-pro"


def test_the_two_presets_set_every_stage_and_are_detected_back():
    cfg = Settings()
    assert cfg.preset == SPEED, "日常测试用 flash，不要默认烧 pro"
    assert cfg.explain_model == "deepseek-v4-flash"

    cfg.apply_preset(QUALITY)
    assert cfg.score_model == cfg.explain_model == cfg.review_model == "deepseek-v4-pro"
    assert cfg.vocab_model == "deepseek-v4-pro"
    assert cfg.explain_thinking == "enabled"
    assert cfg.resolved_preset() == QUALITY

    # Segmentation is free and local; a preset must never drag it up to pro.
    assert cfg.segment_model == "deepseek-v4-flash"

    # Hand-editing one stage has to read back as 自定义, not keep claiming 质量优先.
    cfg.score_model = "deepseek-v4-flash"
    assert cfg.resolved_preset() == CUSTOM


def test_both_presets_think_as_deeply_as_the_model_allows():
    """速度优先 = 快模型 + 最深思考；质量优先 = 强模型 + 最深思考.

    The preset picks the *model*, not the amount of thinking. Both run at the deepest
    setting the chosen model has, on every stage including vocab.

    This reverses what 决策 9 concluded, and does so knowingly. 决策 9 records vocab, left
    to reason freely, spending its whole token budget thinking and emitting no JSON at
    all — it really did die on item 9 of a real run. The guard is no longer a capped
    effort but a capped *output*: effective_max_tokens triples the cap whenever a stage
    runs at its model's deepest level (see test_deep_effort_grows_the_token_cap), and
    require_parsed fails loudly on a truncated reply rather than storing half a word list.
    If truncation comes back, this is the test to change — pin vocab to the standard level.
    """
    cfg = Settings()
    for preset, model in ((SPEED, "deepseek-v4-flash"), (QUALITY, "deepseek-v4-pro")):
        cfg.apply_preset(preset)
        for stage in ("score", "review", "explain", "vocab", "enrich"):
            assert getattr(cfg, f"{stage}_model") == model, f"{preset}/{stage}"
            assert getattr(cfg, f"{stage}_effort") == "max", f"{preset}/{stage} 应该用最深的档"
            assert getattr(cfg, f"{stage}_thinking") == "enabled"


def test_the_deep_preset_adapts_to_each_provider():
    """「最深」是各家自己的最深，不是写死的 max.

    DeepSeek's deepest is `max`; OpenAI's and Claude's is `xhigh`; GLM has no strength
    dial at all and must be sent no level whatsoever — sending `reasoning_effort` to it
    is an unknown field, and drawing a dial for it in the UI would be the decoration
    决策 26 was written about.
    """
    assert preset_values(QUALITY, "deepseek")["explain_effort"] == "max"
    assert preset_values(QUALITY, "openai")["explain_effort"] == "xhigh"
    assert preset_values(QUALITY, "anthropic")["explain_effort"] == "xhigh"
    assert preset_values(QUALITY, "zhipu")["explain_effort"] == ""
    assert efforts_for("zhipu", "glm-5.2") == (), "GLM 没有强度档，不能给它画一个"

    # And each preset must name a model that provider actually serves.
    for provider in ("deepseek", "openai", "anthropic", "zhipu", "qwen"):
        for preset in (SPEED, QUALITY):
            model = preset_values(preset, provider)["explain_model"]
            assert model in model_ids_for(provider), f"{provider}/{preset} -> {model}"


def test_switching_provider_re_derives_the_models():
    """A model name means nothing to a different vendor.

    Keeping `deepseek-v4-pro` after switching to Claude would send Anthropic a model it
    has never heard of, and the run would die on a 404 long after the teacher walked away.
    """
    cfg = Settings()
    assert cfg.explain_model == "deepseek-v4-flash"

    cfg.apply_provider("anthropic")
    assert cfg.provider == "anthropic"
    assert cfg.explain_model in model_ids_for("anthropic")
    assert "deepseek" not in cfg.explain_model
    assert cfg.base_url == pv.get("anthropic").base_url
    # The preset survives the switch — it is a preference, not a model name.
    assert cfg.resolved_preset() == SPEED


def test_an_effort_saved_against_one_provider_is_folded_onto_another():
    # DeepSeek maps low and medium onto high, and xhigh onto max (决策 26).
    assert efforts_for(DEEPSEEK, "deepseek-v4-pro") == ("high", "max")
    for legacy in ("none", "low", "medium"):
        assert normalize_effort(legacy, DEEPSEEK, "deepseek-v4-pro") == "high"
    assert normalize_effort("xhigh", DEEPSEEK, "deepseek-v4-pro") == "max"
    assert normalize_effort("max", DEEPSEEK, "deepseek-v4-pro") == "max"
    assert normalize_effort("nonsense", DEEPSEEK, "deepseek-v4-pro") == "max"

    # A `max` saved against DeepSeek, arriving at a model with no dial at all, must
    # become nothing — not `max`, which GLM would not recognise.
    assert normalize_effort("max", "zhipu", "glm-5.2") == ""


def test_a_settings_file_carrying_an_old_effort_level_still_loads():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({
            "version": SCHEMA_VERSION,  # past the migration, so only the coercion can save it
            "provider": "deepseek",
            "score_model": "deepseek-v4-pro",
            "explain_model": "deepseek-v4-pro",
            "score_effort": "medium",
            "explain_effort": "xhigh",
        }), encoding="utf-8")

        cfg = Settings.load(path)
        assert cfg.score_effort == "high"
        assert cfg.explain_effort == "max"


def test_a_settings_file_naming_a_provider_that_no_longer_exists_still_loads():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({
            "version": SCHEMA_VERSION,
            "provider": "some-vendor-we-dropped",
        }), encoding="utf-8")
        cfg = Settings.load(path)
        assert cfg.provider == pv.DEFAULT_PROVIDER
        assert cfg.explain_model in model_ids_for(pv.DEFAULT_PROVIDER)


def test_every_preset_value_reaches_the_pipeline():
    pipe = _pipeline()

    for provider in ("deepseek", "openai", "anthropic", "zhipu", "qwen"):
        for preset in (SPEED, QUALITY):
            args = pipe.parse_args(["input_docx", "--provider", provider, "--preset", preset])
            for key, value in preset_values(preset, provider).items():
                attr = key.replace("_effort", "_reasoning_effort")
                assert getattr(args, attr) == value, f"--preset {preset} did not reach {attr} on {provider}"

    # An explicit flag has to beat the preset, or "quality but score with flash"
    # would be impossible to express.
    args = pipe.parse_args(["input_docx", "--preset", SPEED, "--explain-model", "deepseek-v4-pro"])
    assert args.explain_model == "deepseek-v4-pro"
    assert args.score_model == "deepseek-v4-flash"


def test_deep_effort_grows_the_token_cap_on_every_provider():
    """决策 9: reasoning tokens are billed against the output quota.

    With every stage now running at its model's deepest setting, a cap sized for a short
    answer would truncate the JSON on every single call. And "deepest" is not the literal
    string `max` — on OpenAI and Claude it is `xhigh`, whose chain of thought is just as
    long. Comparing against `"max"` would leave those two running the deep preset under
    the shallow preset's cap, which is 决策 9 wearing a new hat.
    """
    pipe = _pipeline()

    base = pipe.parse_args(["input_docx"]).vocab_max_tokens  # speed preset, already deep
    for provider in ("deepseek", "openai", "anthropic"):
        args = pipe.parse_args(["input_docx", "--provider", provider, "--preset", QUALITY])
        assert args.vocab_max_tokens > 22_000, f"{provider}: 深度档没有放大 token 上限"
        assert args.explain_max_tokens > 16_000, f"{provider}: 深度档没有放大 token 上限"
    assert base > 22_000, "速度优先也是深度档，上限一样要放大"

    # The cap must never exceed what the model will accept: asking glm-5.2 for 64000
    # output tokens when it caps at 32000 is just a 400.
    args = pipe.parse_args(["input_docx", "--provider", "zhipu", "--preset", QUALITY])
    assert args.vocab_max_tokens <= pv.model_spec("zhipu", args.vocab_model).max_output

    # Picking the standard level by hand leaves the cap alone.
    args = pipe.parse_args(["input_docx", "--vocab-reasoning-effort", "high"])
    assert args.vocab_max_tokens == 22_000


def test_vocab_has_real_headroom_over_the_worst_measured_call():
    """Measured, not guessed.

    A real run of the three sample papers at the deep setting produced, per call:
    25,929 / 27,990 / 38,934 output tokens — of which 94% was reasoning. At the old
    16,000 cap every one of them would have been truncated, and at 48,000 the worst had
    only 19% left.

    So the cap is sized against the number we actually observed, with room for a paper
    somewhat longer than any of the three. If this assertion ever has to be relaxed, the
    honest fix is a shallower effort, not a bigger budget — see 决策 31.
    """
    pipe = _pipeline()
    worst_observed = 38_934

    for preset in (SPEED, QUALITY):
        cap = pipe.parse_args(["input_docx", "--preset", preset]).vocab_max_tokens
        assert cap >= worst_observed * 1.3, (
            f"{preset}: vocab 上限 {cap} 对实测最差一次 {worst_observed} 没有留出足够余量"
        )


def test_every_model_a_preset_offers_has_a_price():
    import usage_report

    for provider in ("deepseek", "openai", "anthropic"):
        for role in (pv.FLASH, pv.PRO):
            model = pv.get(provider).role_model(role)
            spec = usage_report.rate(model, provider)
            assert spec is not None, f"no price for {provider}/{model}: the cost line would lie"
            assert spec.hit < spec.miss < spec.out


def test_an_unpriced_model_is_reported_as_unknown_not_billed_at_someone_elses_rate():
    """决策 20: the old code priced anything it did not recognise at pro's rate.

    A flash run came out ~3x too expensive. With six providers and model ids that churn,
    an unknown model is now routine — so it has to read as 未配价格, not as a number.
    """
    import usage_report

    assert usage_report.rate("a-model-nobody-has-priced", "custom") is None
    assert usage_report.price(1000, 1000, 1000, "a-model-nobody-has-priced", "custom") is None

    priced = usage_report.price_call(
        {"prompt_tokens": 100, "completion_tokens": 10}, "a-model-nobody-has-priced", "custom"
    )
    assert priced.unpriced == 1 and priced.cost_usd == 0.0
    assert not priced.priced


def test_cost_is_priced_from_cache_hits_not_just_totals():
    """A cached prompt token is ~120x cheaper on v4-pro; ignoring that is a 100x error."""
    import usage_report

    usage = {"prompt_tokens": 10_000, "prompt_cache_hit_tokens": 9_000,
             "prompt_cache_miss_tokens": 1_000, "completion_tokens": 1_000}
    priced = usage_report.price_call(usage, "deepseek-v4-pro", DEEPSEEK)
    assert priced.cached == 9_000 and priced.fresh == 1_000
    assert abs(priced.hit_rate - 0.9) < 1e-9

    all_fresh = usage_report.price_call(
        {"prompt_tokens": 10_000, "prompt_cache_miss_tokens": 10_000, "completion_tokens": 1_000},
        "deepseek-v4-pro",
        DEEPSEEK,
    )
    assert priced.cost_usd < all_fresh.cost_usd, "cache hits must actually reduce the quoted cost"
